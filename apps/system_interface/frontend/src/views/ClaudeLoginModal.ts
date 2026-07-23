/**
 * Modal that walks the user through signing Claude in inside a mind. It is
 * the sole auth surface for a workspace. Five sign-in paths:
 *
 * - Claude subscription (primary): drive `claude auth login --claudeai`
 *   via the backend's PTY subprocess. The CLI stores a credential that
 *   running claudes re-read on their next API call, so a fresh workspace
 *   signs in with NO agent restart; when managed settings-env keys are
 *   active they are cleared and the agents restarted (the switching case).
 * - Sign in with Imbue: ask the desktop shell (via a `minds:open-ai-keys-page`
 *   window message to its content relay, keyed by this workspace's host id)
 *   to open its key-mint page over this window, then paste the copied
 *   env-style blob into a textarea. The relay acks the message; with no ack
 *   (plain browser / share tunnel) an alert explains that the desktop app
 *   is required.
 * - Raw API key: paste a `sk-ant-...` value (wrapped into an env-style
 *   line client-side).
 * - Get a long-lived token: `claude setup-token` mints a 1-year token
 *   written into the settings env block (restarts the agents).
 * - Anthropic Console (API billing): `claude auth login --console`; its
 *   key lives in `.claude.json`, so it always restarts the agents.
 *
 * The paste paths (Imbue blob, API key, subtle direct-token affordance)
 * hit one strict backend endpoint (`/api/claude-auth/submit-credentials`),
 * which writes the settings env block and restarts the mind's claude
 * agents; restarts run in the background and are rendered as a step
 * checklist driven by the status endpoint's restart_* fields.
 *
 * The modal is a single app-level instance: global auth state
 * (models/ClaudeAuth.ts) opens it on load-time status failure, on any
 * transcript auth-error, and from the persistent "Agent auth" entry in the
 * chat footer. A muted header line shows how the mind is currently signed
 * in (derived server-side from the settings env content, folding in
 * `claude auth status` for the credentials-based browser sign-ins).
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { claudeLogoIcon, icon, loginSpinnerIcon, warningIcon } from "./icons";

interface ClaudeAuthStatus {
  logged_in: boolean;
  auth_method?: string | null;
  api_provider?: string | null;
  email?: string | null;
  org_id?: string | null;
  org_name?: string | null;
  subscription_type?: string | null;
  auth_mode?: string;
  masked_key_suffix?: string | null;
  workspace_host_id?: string | null;
  restart_phase?: string | null;
  restart_detail?: string | null;
  restart_error?: string | null;
  restart_reason?: string | null;
}

// Which PTY-driven browser flow the awaiting screen is running: the primary
// subscription sign-in, the Console sign-in, or the long-lived-token minting.
type AuthFlow = "claudeai" | "console" | "setup_token";

interface SetupTokenStartResponse {
  session_id: string;
  oauth_url: string;
}

interface SetupTokenPollResponse {
  is_complete: boolean;
  status?: ClaudeAuthStatus | null;
}

type Mode =
  | "select_provider"
  | "api_key_form"
  | "imbue_form"
  | "awaiting_setup_token"
  | "verifying"
  | "applying"
  | "success"
  | "error";

// How often the modal asks the backend whether `claude setup-token` has
// minted the token yet while the awaiting screen is up.
const SETUP_TOKEN_POLL_INTERVAL_MS = 2000;

// How often the modal refreshes the status endpoint while the background
// agent restart runs (the "applying" checklist screen).
const APPLYING_POLL_INTERVAL_MS = 1000;

export interface ClaudeLoginModalAttrs {
  // Called when the user closes the modal -- either after a successful
  // sign-in flow ("Done" button) or via the close affordance before
  // signing in. A subsequent auth-error event will reopen it.
  onDismiss: () => void;
}

// How long to wait for the desktop shell's relay to acknowledge an
// open-the-Imbue-key-page request (see openImbueMintPage). The relay acks
// immediately on receipt, so anything beyond a few event-loop turns means no
// relay is listening -- this page is not being viewed inside the desktop app.
const MINT_PAGE_ACK_TIMEOUT_MS = 300;

export function ClaudeLoginModal(): m.Component<ClaudeLoginModalAttrs> {
  let mode: Mode = "select_provider";
  let activeFlow: AuthFlow = "claudeai";
  let sessionId: string | null = null;
  let oauthUrl: string | null = null;
  let code = "";
  let apiKey = "";
  let apiKeyRevealed = false;
  let imbueBlob = "";
  let directToken = "";
  // Whether the direct-token affordance on the awaiting screen is
  // expanded. Collapsed by default: it is a developer shortcut, unlike the
  // always-visible paste-code input.
  let tokenPasteExpanded = false;
  let urlCopied = false;
  // Set when a clipboard write was attempted but rejected (insecure context,
  // denied permission). Drives the "Failed to copy" label and the raw-URL
  // fallback block so the user can still select and copy the link by hand.
  let urlCopyFailed = false;
  let urlCopiedResetHandle: ReturnType<typeof setTimeout> | null = null;
  // Pending ack handshake for the "Open the Imbue key page" relay request
  // (see openImbueMintPage). Cleared on ack, timeout, or modal teardown.
  let mintAckTimer: ReturnType<typeof setTimeout> | null = null;
  let mintAckListener: ((event: MessageEvent) => void) | null = null;
  let pollHandle: ReturnType<typeof setInterval> | null = null;
  let pollInFlight = false;
  // Status polling for the "applying" screen: the background agent restart
  // reports its progress through the status endpoint's restart_* fields.
  let applyingPollHandle: ReturnType<typeof setInterval> | null = null;
  let applyingPollInFlight = false;
  let applyingStatus: ClaudeAuthStatus | null = null;
  let errorMessage: string | null = null;
  let verifyingTitle = "Working...";
  let verifyingDetail: string | null = null;
  let successStatus: ClaudeAuthStatus | null = null;
  // Fetched when the modal opens; drives the muted "currently signed in
  // via ..." header on the provider-selection screen and the Imbue
  // mint-page link (which needs the workspace host id).
  let currentStatus: ClaudeAuthStatus | null = null;
  let attrsRef: ClaudeLoginModalAttrs | null = null;
  // Whether the "Other ways to sign in" section on the provider-selection
  // screen is expanded. Collapsed by default so the Claude subscription
  // path -- the option most users want -- carries the visual weight.
  let alternativesExpanded = false;

  function clearError(): void {
    errorMessage = null;
  }

  function setError(message: string): void {
    stopPolling();
    stopApplyingPoll();
    errorMessage = message;
    mode = "error";
    m.redraw();
  }

  // Surface a failure inline within a form, where the user can simply
  // re-submit in place, instead of swapping to the full-screen `error`
  // view. Setup-token failures do NOT use this: a failed session is
  // consumed backend-side, so they route to `setError` (the full "Start
  // over" screen).
  function setInlineError(message: string, formMode: "api_key_form" | "imbue_form" | "awaiting_setup_token"): void {
    errorMessage = message;
    mode = formMode;
    m.redraw();
  }

  function startVerifying(title: string, detail: string | null): void {
    stopPolling();
    verifyingTitle = title;
    verifyingDetail = detail;
    mode = "verifying";
    m.redraw();
  }

  function loadCurrentStatus(): void {
    // The header line is progressive enhancement; any failure (including a
    // synchronous one from environments without a DOM, e.g. unit tests)
    // just leaves it blank.
    let statusRequest: Promise<ClaudeAuthStatus>;
    try {
      statusRequest = m.request<ClaudeAuthStatus>({ method: "GET", url: apiUrl("/api/claude-auth/status") });
    } catch {
      currentStatus = null;
      return;
    }
    void statusRequest
      .then((status) => {
        currentStatus = status;
        m.redraw();
      })
      .catch(() => {
        currentStatus = null;
      });
  }

  function stopPolling(): void {
    if (pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
    pollInFlight = false;
  }

  function startPolling(): void {
    stopPolling();
    pollHandle = setInterval(() => {
      void pollSetupToken();
    }, SETUP_TOKEN_POLL_INTERVAL_MS);
  }

  function stopApplyingPoll(): void {
    if (applyingPollHandle !== null) {
      clearInterval(applyingPollHandle);
      applyingPollHandle = null;
    }
    applyingPollInFlight = false;
  }

  // Route a successful credential submit: the backend has written the
  // settings env and kicked the agent restart onto a background thread, so
  // the returned status carries the restart's initial progress. Show the
  // step checklist and follow the restart via the status endpoint until it
  // reports done (success screen) or failed (error screen).
  function enterApplyingOrSuccess(status: ClaudeAuthStatus): void {
    if (status.restart_phase === "failed") {
      setError(status.restart_error ?? "Restarting the agents failed.");
      return;
    }
    if (status.restart_phase === "done" || status.restart_phase == null) {
      successStatus = status;
      mode = "success";
      m.redraw();
      return;
    }
    applyingStatus = status;
    mode = "applying";
    stopApplyingPoll();
    applyingPollHandle = setInterval(() => {
      void pollApplying();
    }, APPLYING_POLL_INTERVAL_MS);
    m.redraw();
  }

  async function pollApplying(): Promise<void> {
    if (mode !== "applying" || applyingPollInFlight) return;
    applyingPollInFlight = true;
    try {
      const status = await m.request<ClaudeAuthStatus>({ method: "GET", url: apiUrl("/api/claude-auth/status") });
      applyingStatus = status;
      if (status.restart_phase === "done") {
        stopApplyingPoll();
        successStatus = status;
        mode = "success";
      } else if (status.restart_phase === "failed") {
        stopApplyingPoll();
        setError(status.restart_error ?? "Restarting the agents failed.");
        return;
      }
      m.redraw();
    } catch {
      // A transient status failure just means this tick learns nothing;
      // keep polling -- the restart continues server-side regardless.
    } finally {
      applyingPollInFlight = false;
    }
  }

  // Endpoint families for the two PTY session kinds: the browser sign-ins
  // (claudeai / console) share the oauth endpoints; the long-lived-token
  // flow keeps its setup-token endpoints.
  function flowBaseUrl(): string {
    return activeFlow === "setup_token" ? "/api/claude-auth/setup-token" : "/api/claude-auth/oauth";
  }

  async function startSetupToken(): Promise<void> {
    activeFlow = "setup_token";
    await startAuthFlowSession("/api/claude-auth/setup-token/start", undefined);
  }

  async function startOauthLogin(provider: "claudeai" | "console"): Promise<void> {
    activeFlow = provider;
    await startAuthFlowSession("/api/claude-auth/oauth/start", { provider });
  }

  async function startAuthFlowSession(path: string, body: object | undefined): Promise<void> {
    clearError();
    startVerifying("Starting sign-in...", "Preparing your sign-in.");
    try {
      // apiUrl is resolved inside the try: it can throw synchronously in a
      // DOM-less environment, and any failure must land on the error screen.
      const response = await m.request<SetupTokenStartResponse>({
        method: "POST",
        url: apiUrl(path),
        body,
      });
      sessionId = response.session_id;
      oauthUrl = response.oauth_url;
      tokenPasteExpanded = false;
      mode = "awaiting_setup_token";
      startPolling();
      m.redraw();
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setError(errResp?.detail ?? "Failed to start the sign-in");
    }
  }

  async function pollSetupToken(): Promise<void> {
    if (sessionId === null || pollInFlight || mode !== "awaiting_setup_token") return;
    pollInFlight = true;
    const polledSessionId = sessionId;
    try {
      const response = await m.request<SetupTokenPollResponse>({
        method: "POST",
        url: apiUrl(`${flowBaseUrl()}/poll`),
        body: { session_id: polledSessionId },
      });
      if (response.is_complete && response.status) {
        stopPolling();
        sessionId = null;
        if (response.status.logged_in) {
          enterApplyingOrSuccess(response.status);
        } else {
          setError("Sign-in completed but Claude still reports it is not authenticated.");
          return;
        }
      }
    } catch (error) {
      // A poll error means the backend session is gone (crashed subprocess,
      // replaced session). There is nothing to retry against in place.
      stopPolling();
      sessionId = null;
      const errResp = (error as { response?: { detail?: string } }).response;
      setError(errResp?.detail ?? "The sign-in session was interrupted");
    } finally {
      pollInFlight = false;
    }
  }

  async function submitSetupTokenCode(): Promise<void> {
    if (!sessionId || !code.trim()) return;
    clearError();
    startVerifying("Verifying code...", "Completing sign-in.");
    const submittedSessionId = sessionId;
    // The backend clears its in-flight session record once the code is
    // sent, so the id we just submitted is consumed regardless of whether
    // auth succeeded. Clear it locally too so a later modal-unmount does
    // not fire a spurious /abort against a discarded session.
    sessionId = null;
    try {
      const status = await m.request<ClaudeAuthStatus>({
        method: "POST",
        url: apiUrl(`${flowBaseUrl()}/submit-code`),
        body: {
          session_id: submittedSessionId,
          code: code.trim(),
        },
      });
      if (status.logged_in) {
        enterApplyingOrSuccess(status);
      } else {
        // A submitted code consumes the single-use session, so there is
        // nothing left to retry in place. Route to the full error screen,
        // whose only action is "Start over" (a fresh sign-in flow).
        setError("Authentication did not succeed.");
      }
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      // Same single-use-session reasoning as the branch above.
      setError(errResp?.detail ?? "Failed to verify code");
    }
  }

  // All three paste paths (API key, Imbue blob, direct token) submit
  // env-var-style lines to the same strict backend endpoint, which writes
  // the settings env block and restarts the mind's claude agents.
  async function submitCredentialLines(
    credentialLines: string,
    verifyingCopy: string,
    failureFormMode: "api_key_form" | "imbue_form" | "awaiting_setup_token",
  ): Promise<void> {
    clearError();
    startVerifying(verifyingCopy, "Applying to this mind and restarting its agents.");
    try {
      const status = await m.request<ClaudeAuthStatus>({
        method: "POST",
        url: apiUrl("/api/claude-auth/submit-credentials"),
        body: {
          credentials: credentialLines,
        },
      });
      if (status.logged_in) {
        enterApplyingOrSuccess(status);
      } else {
        setInlineError("Claude did not accept the credentials. Double-check and try again.", failureFormMode);
      }
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setInlineError(errResp?.detail ?? "Failed to save credentials", failureFormMode);
    }
  }

  function submitApiKey(): void {
    if (!apiKey.trim()) return;
    void submitCredentialLines(`ANTHROPIC_API_KEY=${apiKey.trim()}`, "Saving your API key...", "api_key_form");
  }

  function submitImbueBlob(): void {
    if (!imbueBlob.trim()) return;
    void submitCredentialLines(imbueBlob, "Saving your Imbue credentials...", "imbue_form");
  }

  function submitDirectToken(): void {
    if (!directToken.trim()) return;
    // The direct-token paste replaces the in-flight setup-token session,
    // so drop that session first.
    abortSetupTokenIfActive();
    void submitCredentialLines(
      `CLAUDE_CODE_OAUTH_TOKEN=${directToken.trim()}`,
      "Saving your token...",
      "awaiting_setup_token",
    );
  }

  function abortSetupTokenIfActive(): void {
    stopPolling();
    if (sessionId !== null) {
      void m.request({ method: "POST", url: apiUrl("/api/claude-auth/abort") });
    }
    sessionId = null;
    oauthUrl = null;
    code = "";
    resetUrlCopied();
  }

  function resetUrlCopied(): void {
    urlCopied = false;
    urlCopyFailed = false;
    if (urlCopiedResetHandle !== null) {
      clearTimeout(urlCopiedResetHandle);
      urlCopiedResetHandle = null;
    }
  }

  async function copyOAuthUrl(): Promise<void> {
    if (!oauthUrl) return;
    try {
      await navigator.clipboard.writeText(oauthUrl);
    } catch {
      // Clipboard access can be denied (insecure context, permissions).
      // Tell the user the copy failed and reveal the raw URL below so they
      // can select and copy it manually. Clear any stale "Link copied"
      // state from a recent successful copy so the UI isn't contradictory.
      urlCopied = false;
      if (urlCopiedResetHandle !== null) {
        clearTimeout(urlCopiedResetHandle);
        urlCopiedResetHandle = null;
      }
      urlCopyFailed = true;
      m.redraw();
      return;
    }
    urlCopyFailed = false;
    urlCopied = true;
    if (urlCopiedResetHandle !== null) clearTimeout(urlCopiedResetHandle);
    urlCopiedResetHandle = setTimeout(() => {
      urlCopied = false;
      urlCopiedResetHandle = null;
      m.redraw();
    }, 2000);
    m.redraw();
  }

  function goBackToProviderSelection(): void {
    abortSetupTokenIfActive();
    stopApplyingPoll();
    apiKey = "";
    apiKeyRevealed = false;
    imbueBlob = "";
    directToken = "";
    clearError();
    loadCurrentStatus();
    mode = "select_provider";
    m.redraw();
  }

  // Tear down a pending open-the-Imbue-key-page handshake (ack listener +
  // fallback timer). Called on ack, on timeout, on a re-click, and on modal
  // teardown so a stale timer can never fire the alert later.
  function clearMintAckWait(): void {
    if (mintAckTimer !== null) {
      clearTimeout(mintAckTimer);
      mintAckTimer = null;
    }
    if (mintAckListener !== null) {
      window.removeEventListener("message", mintAckListener);
      mintAckListener = null;
    }
  }

  function openImbueMintPage(): void {
    // The mint page is served by the Minds desktop app, whose origin this
    // workspace page cannot know (the app's backend listens on a random
    // per-run port). Ask the desktop shell to open it over this window via
    // the content-relay postMessage channel; the relay acks immediately, so
    // a missing ack means this page is not being viewed inside the desktop
    // app (plain browser or the share tunnel) and the mint page is
    // unreachable from this browser.
    clearMintAckWait();
    const onAck = (event: MessageEvent): void => {
      if (event.source !== window) return;
      const data: unknown = event.data;
      if (typeof data !== "object" || data === null) return;
      if ((data as { type?: unknown }).type !== "minds:open-ai-keys-ack") return;
      clearMintAckWait();
    };
    mintAckListener = onAck;
    window.addEventListener("message", onAck);
    mintAckTimer = setTimeout(() => {
      clearMintAckWait();
      window.alert(
        "The Imbue key page is part of the Minds desktop app. Open this workspace from the desktop app on your computer to mint a key, then paste it here.",
      );
    }, MINT_PAGE_ACK_TIMEOUT_MS);
    window.postMessage({ type: "minds:open-ai-keys-page", hostId: currentStatus?.workspace_host_id ?? "" }, "*");
  }

  // ----- Renderers -----

  function describeCurrentMode(status: ClaudeAuthStatus | null): string | null {
    if (status === null) return null;
    const suffix = status.masked_key_suffix ? ` (...${status.masked_key_suffix})` : "";
    if (status.auth_mode === "subscription") {
      return status.email
        ? `Currently signed in with your Claude subscription (${status.email}).`
        : "Currently signed in with your Claude subscription.";
    }
    if (status.auth_mode === "console") return "Currently signed in with your Anthropic Console account.";
    if (status.auth_mode === "imbue") return `Currently signed in with Imbue${suffix}.`;
    if (status.auth_mode === "api_key") return `Currently signed in with an API key${suffix}.`;
    if (status.logged_in) return "Currently signed in.";
    return "Not signed in.";
  }

  // The provider-selection screen leads with the Claude subscription as the
  // recommended default -- a logo, headline, and full-width primary button --
  // and tucks the Imbue and API-key paths behind a collapsed "Other ways to
  // sign in" disclosure so they don't compete for attention.
  function renderProviderSelection(): m.Vnode {
    const currentModeLine = describeCurrentMode(currentStatus);
    return m("div.claude-login-select", [
      currentModeLine !== null ? m("p.claude-login-current-mode", currentModeLine) : null,
      m("div.claude-login-primary", [
        m.trust(claudeLogoIcon()),
        m("h3.claude-login-primary-headline", "Sign in with your Claude subscription"),
        m(
          "p.claude-login-primary-sub",
          "Connect your Claude.ai account to use your Pro or Max plan quota in this mind.",
        ),
        m(
          "button.claude-login-button.claude-login-button--primary.claude-login-button--block",
          { type: "button", onclick: () => void startOauthLogin("claudeai") },
          "Continue with Claude subscription",
        ),
      ]),
      m("div.claude-login-alts", [
        m(
          "button.claude-login-alts-toggle",
          {
            type: "button",
            "aria-expanded": String(alternativesExpanded),
            onclick: () => {
              alternativesExpanded = !alternativesExpanded;
              m.redraw();
            },
          },
          [
            m("span", "Other ways to sign in"),
            m(
              `span.claude-login-alts-caret${alternativesExpanded ? ".claude-login-alts-caret--open" : ""}`,
              m.trust(icon("chevron-down", { size: 14 })),
            ),
          ],
        ),
        alternativesExpanded
          ? m("div.claude-login-alts-list", [
              m(
                "button.claude-login-alt",
                {
                  type: "button",
                  onclick: () => {
                    mode = "imbue_form";
                    m.redraw();
                  },
                },
                [
                  m("span.claude-login-alt-text", [
                    m("span.claude-login-alt-name", "Sign in with Imbue"),
                    m(
                      "span.claude-login-alt-desc",
                      "Use an Imbue account to pay per token, no Claude account needed.",
                    ),
                  ]),
                  m("span.claude-login-alt-go", m.trust(icon("chevron-right", { size: 18 }))),
                ],
              ),
              m(
                "button.claude-login-alt",
                {
                  type: "button",
                  onclick: () => {
                    mode = "api_key_form";
                    m.redraw();
                  },
                },
                [
                  m("span.claude-login-alt-text", [
                    m("span.claude-login-alt-name", "Use an API key"),
                    m("span.claude-login-alt-desc", "Paste a raw sk-ant-... API key."),
                  ]),
                  m("span.claude-login-alt-go", m.trust(icon("chevron-right", { size: 18 }))),
                ],
              ),
              m(
                "button.claude-login-alt",
                {
                  type: "button",
                  onclick: () => void startSetupToken(),
                },
                [
                  m("span.claude-login-alt-text", [
                    m("span.claude-login-alt-name", "Get a long-lived token"),
                    m("span.claude-login-alt-desc", "Mint a 1-year subscription token (restarts this mind's agents)."),
                  ]),
                  m("span.claude-login-alt-go", m.trust(icon("chevron-right", { size: 18 }))),
                ],
              ),
              m(
                "button.claude-login-alt",
                {
                  type: "button",
                  onclick: () => void startOauthLogin("console"),
                },
                [
                  m("span.claude-login-alt-text", [
                    m("span.claude-login-alt-name", "Anthropic Console (API billing)"),
                    m(
                      "span.claude-login-alt-desc",
                      "Sign in with a Console account to pay per token (restarts this mind's agents).",
                    ),
                  ]),
                  m("span.claude-login-alt-go", m.trust(icon("chevron-right", { size: 18 }))),
                ],
              ),
            ])
          : null,
      ]),
    ]);
  }

  function renderApiKeyForm(): m.Vnode[] {
    return [
      m("p.claude-login-lead", "Paste an Anthropic API key. It's saved to this mind's shared Claude settings."),
      m("div.claude-login-field", [
        m("label.claude-login-step-label", { for: "claude-login-api-key-input" }, [
          m("span.claude-login-step-num", "1"),
          "Your Anthropic API key",
        ]),
        m("div.claude-login-input-wrap", [
          m("input.claude-login-input.claude-login-input--mono.claude-login-input--with-action", {
            id: "claude-login-api-key-input",
            type: apiKeyRevealed ? "text" : "password",
            placeholder: "sk-ant-...",
            value: apiKey,
            spellcheck: false,
            autocomplete: "off",
            oninput: (event: InputEvent) => {
              apiKey = (event.target as HTMLInputElement).value;
            },
            onkeydown: (event: KeyboardEvent) => {
              if (event.key === "Enter" && apiKey.trim()) {
                event.preventDefault();
                submitApiKey();
              }
            },
          }),
          m(
            "button.claude-login-input-action",
            {
              type: "button",
              onclick: () => {
                apiKeyRevealed = !apiKeyRevealed;
                m.redraw();
              },
              "aria-label": apiKeyRevealed ? "Hide API key" : "Show API key",
            },
            apiKeyRevealed ? "Hide" : "Show",
          ),
        ]),
        m("p.claude-login-helper", "You can find or create API keys at console.anthropic.com."),
      ]),
    ];
  }

  function renderImbueForm(): m.Vnode[] {
    return [
      m(
        "p.claude-login-lead",
        "Get credentials from the Minds desktop app, then paste them here. Your usage is billed to your Imbue account.",
      ),
      m("div.claude-login-step", [
        m("div.claude-login-step-label", [m("span.claude-login-step-num", "1"), "Get your credentials"]),
        m(
          "a.claude-login-button.claude-login-button--primary.claude-login-button--block.claude-login-button--link",
          {
            href: "#",
            onclick: (event: MouseEvent) => {
              event.preventDefault();
              openImbueMintPage();
            },
          },
          [m("span", "Open the Imbue key page"), m.trust(icon("external-link", { size: 15 }))],
        ),
        m(
          "p.claude-login-helper",
          "The key page creates a key for this workspace and copies the credentials to your clipboard.",
        ),
      ]),
      m("div.claude-login-step", [
        m("label.claude-login-step-label", { for: "claude-login-imbue-blob-input" }, [
          m("span.claude-login-step-num", "2"),
          "Paste your credentials",
        ]),
        m("textarea.claude-login-input.claude-login-input--mono.claude-login-textarea", {
          id: "claude-login-imbue-blob-input",
          rows: 3,
          placeholder: "ANTHROPIC_BASE_URL=...\nANTHROPIC_API_KEY=sk-...",
          value: imbueBlob,
          spellcheck: false,
          autocomplete: "off",
          oninput: (event: InputEvent) => {
            imbueBlob = (event.target as HTMLTextAreaElement).value;
          },
        }),
      ]),
    ];
  }

  function renderAwaitingSetupToken(): Array<m.Vnode | null> {
    return [
      m("p.claude-login-lead", "Approve access in your browser, then paste the code it shows you."),
      m("div.claude-login-step", [
        m("div.claude-login-step-label", [m("span.claude-login-step-num", "1"), "Open the sign-in page"]),
        m(
          "a.claude-login-button.claude-login-button--primary.claude-login-button--block.claude-login-button--link",
          {
            href: oauthUrl,
            target: "_blank",
            rel: "noopener noreferrer",
          },
          [m("span", "Open sign-in page"), m.trust(icon("external-link", { size: 15 }))],
        ),
        m("p.claude-login-copylink", [
          "Didn't open? ",
          m(
            "button.claude-login-copylink-action",
            {
              type: "button",
              onclick: () => {
                void copyOAuthUrl();
              },
            },
            urlCopied ? "Link copied" : urlCopyFailed ? "Failed to copy" : "Copy the link",
          ),
          urlCopied ? "" : urlCopyFailed ? " — copy this link manually:" : " and paste it into your browser.",
        ]),
        // When the clipboard write was rejected, surface the raw URL so the
        // user is never stranded without a way to reach the sign-in page.
        urlCopyFailed && oauthUrl !== null ? m("div.claude-login-rawurl", { tabindex: 0 }, oauthUrl) : null,
      ]),
      // The approval page shows a CODE#STATE string -- pasting it here is
      // the primary way to finish, so the input is always visible. (The
      // background poll still runs silently: if the CLI's own polling ever
      // completes the flow first, the modal just finishes early.)
      m("div.claude-login-step", [
        m("div.claude-login-step-label", [m("span.claude-login-step-num", "2"), "Approve, then paste the code shown"]),
        m("div.claude-login-subtle-body", [
          m("input.claude-login-input.claude-login-input--mono", {
            id: "claude-login-code-input",
            type: "text",
            placeholder: "CODE#STATE",
            value: code,
            spellcheck: false,
            autocomplete: "off",
            oninput: (event: InputEvent) => {
              code = (event.target as HTMLInputElement).value;
            },
            onkeydown: (event: KeyboardEvent) => {
              if (event.key === "Enter" && code.trim()) {
                event.preventDefault();
                void submitSetupTokenCode();
              }
            },
          }),
          m(
            "button.claude-login-button.claude-login-button--primary",
            {
              type: "button",
              disabled: !code.trim(),
              onclick: () => {
                void submitSetupTokenCode();
              },
            },
            "Verify code",
          ),
        ]),
      ]),
      // Subtle direct-token affordance: developers who already have a
      // long-lived token can skip the browser flow entirely. Only offered
      // on the token-minting flow -- it writes the settings env, which
      // would be misleading on the credentials-based sign-ins.
      activeFlow !== "setup_token"
        ? null
        : m("div.claude-login-subtle", [
            tokenPasteExpanded
              ? m("div.claude-login-subtle-body", [
                  m("input.claude-login-input.claude-login-input--mono", {
                    id: "claude-login-token-input",
                    type: "password",
                    placeholder: "sk-ant-oat01-...",
                    value: directToken,
                    spellcheck: false,
                    autocomplete: "off",
                    oninput: (event: InputEvent) => {
                      directToken = (event.target as HTMLInputElement).value;
                    },
                    onkeydown: (event: KeyboardEvent) => {
                      if (event.key === "Enter" && directToken.trim()) {
                        event.preventDefault();
                        submitDirectToken();
                      }
                    },
                  }),
                  m(
                    "button.claude-login-button.claude-login-button--primary",
                    {
                      type: "button",
                      disabled: !directToken.trim(),
                      onclick: () => {
                        submitDirectToken();
                      },
                    },
                    "Use token",
                  ),
                ])
              : m(
                  "button.claude-login-subtle-toggle",
                  {
                    type: "button",
                    onclick: () => {
                      tokenPasteExpanded = true;
                      m.redraw();
                    },
                  },
                  "Already have a token? Paste it instead",
                ),
          ]),
    ];
  }

  // The step checklist shown while the background credential apply runs,
  // tracking the status endpoint's restart_phase. The leading (already
  // completed) steps depend on why the restart is happening: a plain
  // credential save, or a switch away from managed keys after a browser
  // sign-in (subscription or Console).
  function renderApplying(): m.Vnode {
    const phase = applyingStatus?.restart_phase ?? "restarting";
    const detail = applyingStatus?.restart_detail ?? null;
    const reason = applyingStatus?.restart_reason ?? "credentials_saved";
    const leadLabels =
      reason === "subscription_switch"
        ? ["Signed in with your subscription", "Removing old credentials"]
        : reason === "console_switch"
          ? ["Signed in with your Anthropic Console account", "Removing old credentials"]
          : ["Credentials saved"];
    const phaseOrder = ["restarting", "finishing"];
    const activeIdx = Math.max(0, phaseOrder.indexOf(phase));
    const tailLabels = ["Restarting agents", "Resuming your agent"];
    const steps: { label: string; state: "done" | "active" | "pending" }[] = [
      ...leadLabels.map((label) => ({ label, state: "done" as const })),
      ...tailLabels.map((label, idx) => ({
        label,
        state: idx < activeIdx ? ("done" as const) : idx === activeIdx ? ("active" as const) : ("pending" as const),
      })),
    ];
    return m("div.claude-login-applying", [
      m(
        "ul.claude-login-checklist",
        steps.map((step) =>
          m(`li.claude-login-checklist-item.claude-login-checklist-item--${step.state}`, [
            m(
              "span.claude-login-checklist-icon",
              step.state === "done"
                ? m.trust(icon("check", { size: 14, strokeWidth: 2.5 }))
                : step.state === "active"
                  ? m.trust(loginSpinnerIcon())
                  : null,
            ),
            m("span.claude-login-checklist-label", step.label),
          ]),
        ),
      ),
      detail !== null ? m("p.claude-login-helper.claude-login-helper--center", detail) : null,
    ]);
  }

  function renderStatus(kind: "loading" | "success" | "error", title: string, detail: string | null): m.Vnode {
    const statusGlyph =
      kind === "loading"
        ? m.trust(loginSpinnerIcon())
        : kind === "success"
          ? m.trust(icon("check", { size: 26, strokeWidth: 2.5 }))
          : m.trust(warningIcon());
    return m("div.claude-login-status", [
      m(`div.claude-login-status-icon.claude-login-status-icon--${kind}`, statusGlyph),
      m("p.claude-login-status-title", title),
      detail !== null ? m("p.claude-login-status-detail", detail) : null,
    ]);
  }

  function renderSuccess(): m.Vnode {
    const status = successStatus;
    const email = status?.email ?? null;
    if (status?.auth_mode === "subscription") {
      const detail = email
        ? `Signed in as ${email} with your Claude subscription.`
        : "Signed in with your Claude subscription.";
      // Only the minted-token flow carries an expiry worth mentioning.
      if (activeFlow === "setup_token") {
        return m("div", [
          renderStatus("success", "All set", detail),
          m("p.claude-login-helper.claude-login-helper--center", "Your sign-in token is valid for about a year."),
        ]);
      }
      return renderStatus("success", "All set", detail);
    }
    let detail: string;
    if (status?.auth_mode === "console") {
      detail = "Signed in with your Anthropic Console account.";
    } else if (status?.auth_mode === "imbue") {
      detail = "Signed in with Imbue.";
    } else if (email) {
      detail = `Signed in as ${email}.`;
    } else {
      detail = "You're signed in.";
    }
    return renderStatus("success", "All set", detail);
  }

  function renderInlineError(): m.Vnode {
    return m("div.claude-login-error-callout", [m.trust(warningIcon(16)), m("span", errorMessage ?? "")]);
  }

  // ----- Layout (header / body / footer) -----

  function titleForMode(): string {
    if (mode === "success") return "Signed in";
    if (mode === "error") return "Something went wrong";
    if (mode === "verifying") return "Just a moment";
    if (mode === "applying") return "Finishing up";
    if (mode === "api_key_form") return "Sign in with API key";
    if (mode === "imbue_form") return "Sign in with Imbue";
    if (mode === "awaiting_setup_token") return "Finish signing in";
    return "Sign in to Claude";
  }

  function renderBody(): m.Vnode | Array<m.Vnode | null> {
    if (mode === "success") return renderSuccess();
    if (mode === "error") {
      return renderStatus("error", "Couldn't complete sign-in", errorMessage ?? "An unexpected error occurred.");
    }
    if (mode === "verifying") return renderStatus("loading", verifyingTitle, verifyingDetail);
    if (mode === "applying") return renderApplying();
    if (mode === "awaiting_setup_token") return renderAwaitingSetupToken();
    if (mode === "api_key_form") return renderApiKeyForm();
    if (mode === "imbue_form") return renderImbueForm();
    return renderProviderSelection();
  }

  function renderFooter(): m.Vnode | null {
    if (mode === "select_provider" || mode === "verifying" || mode === "applying") return null;
    if (mode === "success") {
      return m("div.claude-login-footer", [
        m(
          "button.claude-login-button.claude-login-button--primary",
          { type: "button", onclick: () => attrsRef?.onDismiss() },
          "Done",
        ),
      ]);
    }
    if (mode === "error") {
      // A sign-in failure (failed setup-token start, or a consumed
      // single-use session) leaves no live session to retry against, so
      // the only forward action is to start the whole flow over. The
      // header close button and backdrop click still dismiss the modal, so
      // a single primary action here is not a dead end.
      return m("div.claude-login-footer", [
        m(
          "button.claude-login-button.claude-login-button--primary.claude-login-button--block",
          { type: "button", onclick: () => goBackToProviderSelection() },
          "Start over",
        ),
      ]);
    }
    if (mode === "api_key_form") {
      return m("div.claude-login-footer.claude-login-footer--spread", [
        m(
          "button.claude-login-button.claude-login-button--ghost",
          { type: "button", onclick: () => goBackToProviderSelection() },
          "Back",
        ),
        m(
          "button.claude-login-button.claude-login-button--primary",
          {
            type: "button",
            disabled: !apiKey.trim(),
            onclick: () => {
              submitApiKey();
            },
          },
          "Save & finish",
        ),
      ]);
    }
    if (mode === "imbue_form") {
      return m("div.claude-login-footer.claude-login-footer--spread", [
        m(
          "button.claude-login-button.claude-login-button--ghost",
          { type: "button", onclick: () => goBackToProviderSelection() },
          "Back",
        ),
        m(
          "button.claude-login-button.claude-login-button--primary",
          {
            type: "button",
            disabled: !imbueBlob.trim(),
            onclick: () => {
              submitImbueBlob();
            },
          },
          "Save & finish",
        ),
      ]);
    }
    // awaiting_setup_token: no primary action -- the flow completes via
    // polling (or one of the subtle affordances, which carry their own
    // buttons). Back returns to provider selection and aborts the session.
    return m("div.claude-login-footer", [
      m(
        "button.claude-login-button.claude-login-button--ghost",
        { type: "button", onclick: () => goBackToProviderSelection() },
        "Back",
      ),
    ]);
  }

  return {
    oncreate(vnode: m.VnodeDOM<ClaudeLoginModalAttrs>) {
      attrsRef = vnode.attrs;
      loadCurrentStatus();
    },

    onupdate(vnode: m.VnodeDOM<ClaudeLoginModalAttrs>) {
      attrsRef = vnode.attrs;
    },

    onremove() {
      abortSetupTokenIfActive();
      stopApplyingPoll();
      clearMintAckWait();
    },

    view() {
      const onClose = (): void => attrsRef?.onDismiss();
      return m(
        "div.claude-login-overlay",
        {
          onclick: (event: MouseEvent) => {
            if (event.target === event.currentTarget) onClose();
          },
        },
        m(
          "div.claude-login-modal",
          {
            role: "dialog",
            "aria-modal": "true",
            "aria-label": "Sign in to Claude",
          },
          [
            m("div.claude-login-header", [
              m("h2.claude-login-title", titleForMode()),
              m(
                "button.claude-login-close",
                { type: "button", onclick: onClose, "aria-label": "Close" },
                m.trust(icon("close", { size: 16 })),
              ),
            ]),
            m(
              "div.claude-login-body",
              mode === "awaiting_setup_token" || mode === "api_key_form" || mode === "imbue_form"
                ? [errorMessage !== null ? renderInlineError() : null, renderBody()]
                : renderBody(),
            ),
            renderFooter(),
          ],
        ),
      );
    },
  };
}
