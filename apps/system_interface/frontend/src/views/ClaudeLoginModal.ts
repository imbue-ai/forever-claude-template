/**
 * Modal that walks the user through signing Claude in inside a mind. It is
 * the sole auth surface for a workspace: credentials live in the `env`
 * block of the shared Claude settings.json and are written only by the
 * backend behind this modal. Three sign-in paths:
 *
 * - Claude subscription (default): drive `claude setup-token` via the PTY
 *   subprocess on the backend. The CLI prints an OAuth URL and then polls
 *   Anthropic itself, so the modal shows the URL and polls the backend
 *   until the 1-year token is minted -- no code paste needed in the normal
 *   flow (a paste-code fallback and a subtle "already have a token" paste
 *   affordance are provided).
 * - Sign in with Imbue: link to the desktop app's key-mint page (keyed by
 *   this workspace's host id), then paste the copied env-style blob into a
 *   textarea. Clicking the link while the workspace is opened remotely
 *   pops an alert to do it from the desktop client instead.
 * - Raw API key: paste a `sk-ant-...` value (wrapped into an env-style
 *   line client-side).
 *
 * All three submission paths hit the same strict backend endpoint
 * (`/api/claude-auth/submit-credentials`), which writes the settings env
 * block and restarts the mind's claude agents.
 *
 * The modal is a single app-level instance: global auth state
 * (models/ClaudeAuth.ts) opens it on load-time status failure, on any
 * transcript auth-error, and from the persistent "Agent auth" entry in the
 * chat footer. A muted header line shows how the mind is currently signed
 * in (derived server-side from the settings env content).
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
}

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
  | "success"
  | "error";

// How often the modal asks the backend whether `claude setup-token` has
// minted the token yet while the awaiting screen is up.
const SETUP_TOKEN_POLL_INTERVAL_MS = 2000;

export interface ClaudeLoginModalAttrs {
  // Called when the user closes the modal -- either after a successful
  // sign-in flow ("Done" button) or via the close affordance before
  // signing in. A subsequent auth-error event will reopen it.
  onDismiss: () => void;
}

// Compute the desktop client's main-app origin from the workspace's own
// origin. Workspaces are served at `<agent-id>.localhost:<port>` by the
// desktop client, whose own UI lives at `localhost:<port>` -- so dropping
// the leading agent label yields the main app. Returns null when the
// workspace is NOT being accessed through the local desktop client (e.g.
// via its Cloudflare tunnel), in which case the mint page is unreachable
// from this browser.
export function computeDesktopAppOrigin(hostname: string, port: string, protocol: string): string | null {
  if (hostname !== "localhost" && !hostname.endsWith(".localhost")) return null;
  const baseHost = hostname === "localhost" ? hostname : hostname.split(".").slice(1).join(".");
  const portSuffix = port ? `:${port}` : "";
  return `${protocol}//${baseHost}${portSuffix}`;
}

export function ClaudeLoginModal(): m.Component<ClaudeLoginModalAttrs> {
  let mode: Mode = "select_provider";
  let sessionId: string | null = null;
  let oauthUrl: string | null = null;
  let code = "";
  let apiKey = "";
  let apiKeyRevealed = false;
  let imbueBlob = "";
  let directToken = "";
  // Whether the paste-code fallback / direct-token affordances on the
  // awaiting screen are expanded. Both stay collapsed by default so the
  // "approve in browser and wait" happy path carries the visual weight.
  let codeEntryExpanded = false;
  let tokenPasteExpanded = false;
  let urlCopied = false;
  // Set when a clipboard write was attempted but rejected (insecure context,
  // denied permission). Drives the "Failed to copy" label and the raw-URL
  // fallback block so the user can still select and copy the link by hand.
  let urlCopyFailed = false;
  let urlCopiedResetHandle: ReturnType<typeof setTimeout> | null = null;
  let pollHandle: ReturnType<typeof setInterval> | null = null;
  let pollInFlight = false;
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

  async function startSetupToken(): Promise<void> {
    clearError();
    startVerifying("Starting sign-in...", "Preparing your Claude subscription sign-in.");
    try {
      const response = await m.request<SetupTokenStartResponse>({
        method: "POST",
        url: apiUrl("/api/claude-auth/setup-token/start"),
      });
      sessionId = response.session_id;
      oauthUrl = response.oauth_url;
      codeEntryExpanded = false;
      tokenPasteExpanded = false;
      mode = "awaiting_setup_token";
      startPolling();
      m.redraw();
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setError(errResp?.detail ?? "Failed to start the subscription sign-in");
    }
  }

  async function pollSetupToken(): Promise<void> {
    if (sessionId === null || pollInFlight || mode !== "awaiting_setup_token") return;
    pollInFlight = true;
    const polledSessionId = sessionId;
    try {
      const response = await m.request<SetupTokenPollResponse>({
        method: "POST",
        url: apiUrl("/api/claude-auth/setup-token/poll"),
        body: { session_id: polledSessionId },
      });
      if (response.is_complete && response.status) {
        stopPolling();
        sessionId = null;
        if (response.status.logged_in) {
          successStatus = response.status;
          mode = "success";
        } else {
          setError("Sign-in completed but Claude still reports it is not authenticated.");
          return;
        }
        m.redraw();
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
        url: apiUrl("/api/claude-auth/setup-token/submit-code"),
        body: {
          session_id: submittedSessionId,
          code: code.trim(),
        },
      });
      if (status.logged_in) {
        successStatus = status;
        mode = "success";
        m.redraw();
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
        successStatus = status;
        mode = "success";
        m.redraw();
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
    apiKey = "";
    apiKeyRevealed = false;
    imbueBlob = "";
    directToken = "";
    clearError();
    loadCurrentStatus();
    mode = "select_provider";
    m.redraw();
  }

  function openImbueMintPage(): void {
    const desktopOrigin = computeDesktopAppOrigin(
      window.location.hostname,
      window.location.port,
      window.location.protocol,
    );
    if (desktopOrigin === null) {
      // Reached via the Cloudflare tunnel (or any non-local route): the
      // desktop client's own UI is not reachable from this browser.
      window.alert(
        "The Imbue key page is part of the Minds desktop app. Open this workspace from the desktop app on your computer to mint a key, then paste it here.",
      );
      return;
    }
    const hostId = currentStatus?.workspace_host_id ?? "";
    const target = `${desktopOrigin}/settings/ai-keys${hostId ? `?workspace=${encodeURIComponent(hostId)}` : ""}`;
    window.open(target, "_blank", "noopener,noreferrer");
  }

  // ----- Renderers -----

  function describeCurrentMode(status: ClaudeAuthStatus | null): string | null {
    if (status === null) return null;
    const suffix = status.masked_key_suffix ? ` (...${status.masked_key_suffix})` : "";
    if (status.auth_mode === "subscription") return "Currently signed in with your Claude subscription.";
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
          { type: "button", onclick: () => void startSetupToken() },
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

  function renderAwaitingSetupToken(): m.Vnode[] {
    return [
      m("p.claude-login-lead", "Approve access in your browser. This screen finishes on its own once you have."),
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
      m("div.claude-login-step", [
        m("div.claude-login-step-label", [m("span.claude-login-step-num", "2"), "Approve in the browser"]),
        m("div.claude-login-waiting", [m.trust(loginSpinnerIcon()), m("span", "Waiting for your approval...")]),
      ]),
      // Paste-code fallback: some sign-in flows show a CODE#STATE string
      // instead of completing on their own.
      m("div.claude-login-subtle", [
        codeEntryExpanded
          ? m("div.claude-login-subtle-body", [
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
            ])
          : m(
              "button.claude-login-subtle-toggle",
              {
                type: "button",
                onclick: () => {
                  codeEntryExpanded = true;
                  m.redraw();
                },
              },
              "The page showed a code? Paste it instead",
            ),
      ]),
      // Subtle direct-token affordance: developers who already have a
      // long-lived token can skip the browser flow entirely.
      m("div.claude-login-subtle", [
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
      return m("div", [
        renderStatus("success", "All set", "Signed in with your Claude subscription."),
        m("p.claude-login-helper.claude-login-helper--center", "Your sign-in token is valid for about a year."),
      ]);
    }
    let detail: string;
    if (status?.auth_mode === "imbue") {
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
    if (mode === "api_key_form") return "Sign in with API key";
    if (mode === "imbue_form") return "Sign in with Imbue";
    if (mode === "awaiting_setup_token") return "Finish signing in";
    return "Sign in to Claude";
  }

  function renderBody(): m.Vnode | m.Vnode[] {
    if (mode === "success") return renderSuccess();
    if (mode === "error") {
      return renderStatus("error", "Couldn't complete sign-in", errorMessage ?? "An unexpected error occurred.");
    }
    if (mode === "verifying") return renderStatus("loading", verifyingTitle, verifyingDetail);
    if (mode === "awaiting_setup_token") return renderAwaitingSetupToken();
    if (mode === "api_key_form") return renderApiKeyForm();
    if (mode === "imbue_form") return renderImbueForm();
    return renderProviderSelection();
  }

  function renderFooter(): m.Vnode | null {
    if (mode === "select_provider" || mode === "verifying") return null;
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
