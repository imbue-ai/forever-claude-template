/**
 * Modal that walks the user through re-authenticating Claude inside a mind
 * when credentials didn't sync from the host. Three sign-in paths:
 *
 * - Claude subscription: drive `claude auth login --claudeai` via the PTY
 *   subprocess on the backend, parse the printed OAuth URL, paste back the
 *   user's CODE#STATE.
 * - Anthropic Console: same flow but `--console`.
 * - Raw API key: paste a `sk-ant-...` value; backend writes it to the host
 *   env file and restarts every running claude agent.
 *
 * The modal is purely reactive and a single app-level instance: global
 * auth state (models/ClaudeAuth.ts) opens it when any agent surfaces an
 * auth-error, and it closes only when the user dismisses it. It does not
 * poll the backend status endpoint, because `claude auth status` reflects
 * the system-interface process's view of auth which can disagree with the
 * already-running agent's cached in-process auth decision.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

interface ClaudeAuthStatus {
  logged_in: boolean;
  auth_method?: string | null;
  api_provider?: string | null;
  email?: string | null;
  org_id?: string | null;
  org_name?: string | null;
  subscription_type?: string | null;
}

interface OAuthStartResponse {
  session_id: string;
  oauth_url: string;
}

type Mode = "select_provider" | "api_key_form" | "awaiting_oauth_code" | "verifying" | "success" | "error";

export interface ClaudeLoginModalAttrs {
  // Called when the user closes the modal -- either after a successful
  // sign-in flow ("Done" button) or via the close affordance before
  // signing in. A subsequent auth-error event will reopen it.
  onDismiss: () => void;
}

function spinnerIcon(): m.Vnode {
  return m("svg.claude-login-spinner", { viewBox: "0 0 24 24", fill: "none", "aria-hidden": "true" }, [
    m("circle", {
      cx: 12,
      cy: 12,
      r: 10,
      stroke: "currentColor",
      "stroke-opacity": 0.18,
      "stroke-width": 3,
    }),
    m("path", {
      d: "M22 12a10 10 0 0 1-10 10",
      stroke: "currentColor",
      "stroke-width": 3,
      "stroke-linecap": "round",
    }),
  ]);
}

function checkIcon(): m.Vnode {
  return m(
    "svg",
    {
      width: 26,
      height: 26,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    m("path", {
      d: "M5 12.5l4.5 4.5L19 7.5",
      stroke: "currentColor",
      "stroke-width": 2.5,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    }),
  );
}

function warningIcon(small = false): m.Vnode {
  const s = small ? 16 : 26;
  return m(
    "svg",
    {
      width: s,
      height: s,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    [
      m("circle", {
        cx: 12,
        cy: 12,
        r: 10,
        stroke: "currentColor",
        "stroke-width": small ? 1.8 : 2,
      }),
      m("path", {
        d: "M12 8v4.5",
        stroke: "currentColor",
        "stroke-width": small ? 1.8 : 2.2,
        "stroke-linecap": "round",
      }),
      m("circle", { cx: 12, cy: 16, r: 0.9, fill: "currentColor" }),
    ],
  );
}

function closeIcon(): m.Vnode {
  return m(
    "svg",
    {
      width: 16,
      height: 16,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    m("path", {
      d: "M6 6l12 12M18 6L6 18",
      stroke: "currentColor",
      "stroke-width": 2,
      "stroke-linecap": "round",
    }),
  );
}

// The official Claude "burst" symbol. Source: Wikimedia Commons
// `File:Claude_AI_symbol.svg`, released under CC0 1.0 (public domain).
// Used to mark the Claude subscription as the recommended sign-in path.
function claudeLogoIcon(): m.Vnode {
  return m(
    "svg.claude-login-logo",
    { viewBox: "0 0 100 100", "aria-hidden": "true" },
    m("path", {
      d: "m19.6 66.5 19.7-11 .3-1-.3-.5h-1l-3.3-.2-11.2-.3L14 53l-9.5-.5-2.4-.5L0 49l.2-1.5 2-1.3 2.9.2 6.3.5 9.5.6 6.9.4L38 49.1h1.6l.2-.7-.5-.4-.4-.4L29 41l-10.6-7-5.6-4.1-3-2-1.5-2-.6-4.2 2.7-3 3.7.3.9.2 3.7 2.9 8 6.1L37 36l1.5 1.2.6-.4.1-.3-.7-1.1L33 25l-6-10.4-2.7-4.3-.7-2.6c-.3-1-.4-2-.4-3l3-4.2L28 0l4.2.6L33.8 2l2.6 6 4.1 9.3L47 29.9l2 3.8 1 3.4.3 1h.7v-.5l.5-7.2 1-8.7 1-11.2.3-3.2 1.6-3.8 3-2L61 2.6l2 2.9-.3 1.8-1.1 7.7L59 27.1l-1.5 8.2h.9l1-1.1 4.1-5.4 6.9-8.6 3-3.5L77 13l2.3-1.8h4.3l3.1 4.7-1.4 4.9-4.4 5.6-3.7 4.7-5.3 7.1-3.2 5.7.3.4h.7l12-2.6 6.4-1.1 7.6-1.3 3.5 1.6.4 1.6-1.4 3.4-8.2 2-9.6 2-14.3 3.3-.2.1.2.3 6.4.6 2.8.2h6.8l12.6 1 3.3 2 1.9 2.7-.3 2-5.1 2.6-6.8-1.6-16-3.8-5.4-1.3h-.8v.4l4.6 4.5 8.3 7.5L89 80.1l.5 2.4-1.3 2-1.4-.2-9.2-7-3.6-3-8-6.8h-.5v.7l1.8 2.7 9.8 14.7.5 4.5-.7 1.4-2.6 1-2.7-.6-5.8-8-6-9-4.7-8.2-.5.4-2.9 30.2-1.3 1.5-3 1.2-2.5-2-1.4-3 1.4-6.2 1.6-8 1.3-6.4 1.2-7.9.7-2.6v-.2H49L43 72l-9 12.3-7.2 7.6-1.7.7-3-1.5.3-2.8L24 86l10-12.8 6-7.9 4-4.6-.1-.5h-.3L17.2 77.4l-4.7.6-2-2 .2-3 1-1 8-5.5Z",
    }),
  );
}

function caretIcon(): m.Vnode {
  return m(
    "svg",
    {
      width: 14,
      height: 14,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    m("path", {
      d: "M6 9l6 6 6-6",
      stroke: "currentColor",
      "stroke-width": 2,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    }),
  );
}

function chevronRightIcon(): m.Vnode {
  return m(
    "svg",
    {
      width: 18,
      height: 18,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    m("path", {
      d: "M9 6l6 6-6 6",
      stroke: "currentColor",
      "stroke-width": 2,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    }),
  );
}

function externalLinkIcon(): m.Vnode {
  return m(
    "svg",
    {
      width: 15,
      height: 15,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    [
      m("path", {
        d: "M14 4h6v6",
        stroke: "currentColor",
        "stroke-width": 2,
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
      }),
      m("path", {
        d: "M20 4l-9 9",
        stroke: "currentColor",
        "stroke-width": 2,
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
      }),
      m("path", {
        d: "M19 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h6",
        stroke: "currentColor",
        "stroke-width": 2,
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
      }),
    ],
  );
}

export function ClaudeLoginModal(): m.Component<ClaudeLoginModalAttrs> {
  let mode: Mode = "select_provider";
  let sessionId: string | null = null;
  let oauthUrl: string | null = null;
  let code = "";
  let apiKey = "";
  let apiKeyRevealed = false;
  let urlCopied = false;
  // Set when a clipboard write was attempted but rejected (insecure context,
  // denied permission). Drives the "Failed to copy" label and the raw-URL
  // fallback block so the user can still select and copy the link by hand.
  let urlCopyFailed = false;
  let urlCopiedResetHandle: ReturnType<typeof setTimeout> | null = null;
  let errorMessage: string | null = null;
  let verifyingTitle = "Working...";
  let verifyingDetail: string | null = null;
  let successStatus: ClaudeAuthStatus | null = null;
  let attrsRef: ClaudeLoginModalAttrs | null = null;
  // Whether the "Other ways to sign in" section on the provider-selection
  // screen is expanded. Collapsed by default so the Claude subscription
  // path -- the option most users want -- carries the visual weight.
  let alternativesExpanded = false;

  function clearError(): void {
    errorMessage = null;
  }

  function setError(message: string): void {
    errorMessage = message;
    mode = "error";
    m.redraw();
  }

  // Surface a failure inline within the form the user was filling, instead
  // of swapping to the full-screen `error` view. Used for submit failures
  // (OAuth code / API key) where the user should stay on the form and
  // retry; `setError` remains for `startOAuth` failures, which have no
  // form to return to.
  function setInlineError(message: string, formMode: "awaiting_oauth_code" | "api_key_form"): void {
    errorMessage = message;
    mode = formMode;
    m.redraw();
  }

  function startVerifying(title: string, detail: string | null): void {
    verifyingTitle = title;
    verifyingDetail = detail;
    mode = "verifying";
    m.redraw();
  }

  async function startOAuth(chosen: "claudeai" | "console"): Promise<void> {
    clearError();
    startVerifying(
      "Starting sign-in...",
      chosen === "claudeai"
        ? "Spawning the Claude subscription OAuth flow."
        : "Spawning the Anthropic Console OAuth flow.",
    );
    try {
      const response = await m.request<OAuthStartResponse>({
        method: "POST",
        url: apiUrl("/api/claude-auth/start"),
        body: { provider: chosen },
      });
      sessionId = response.session_id;
      oauthUrl = response.oauth_url;
      mode = "awaiting_oauth_code";
      m.redraw();
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setError(errResp?.detail ?? "Failed to start OAuth login");
    }
  }

  async function submitOAuthCode(): Promise<void> {
    if (!sessionId || !code.trim()) return;
    clearError();
    startVerifying("Verifying code...", "Completing sign-in.");
    const submittedSessionId = sessionId;
    // The backend's submit_oauth_code clears its in-flight session record
    // unconditionally (in its finally block) once the code is sent, so the
    // id we just submitted is consumed regardless of whether auth succeeded.
    // Clear it locally too so a later modal-unmount (e.g. Done after a
    // successful sign-in) does not fire a spurious /abort against a session
    // the backend has already discarded.
    sessionId = null;
    try {
      const status = await m.request<ClaudeAuthStatus>({
        method: "POST",
        url: apiUrl("/api/claude-auth/submit-code"),
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
        setInlineError("Authentication did not succeed. Please try again.", "awaiting_oauth_code");
      }
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setInlineError(errResp?.detail ?? "Failed to verify code", "awaiting_oauth_code");
    }
  }

  async function submitApiKey(): Promise<void> {
    if (!apiKey.trim()) return;
    clearError();
    startVerifying("Saving your API key...", "Applying it to this mind.");
    try {
      const status = await m.request<ClaudeAuthStatus>({
        method: "POST",
        url: apiUrl("/api/claude-auth/submit-api-key"),
        body: {
          api_key: apiKey.trim(),
        },
      });
      if (status.logged_in) {
        successStatus = status;
        mode = "success";
        m.redraw();
      } else {
        setInlineError("Anthropic did not accept the API key. Double-check and try again.", "api_key_form");
      }
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setInlineError(errResp?.detail ?? "Failed to save API key", "api_key_form");
    }
  }

  function abortOAuthIfActive(): void {
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
    abortOAuthIfActive();
    apiKey = "";
    apiKeyRevealed = false;
    clearError();
    mode = "select_provider";
    m.redraw();
  }

  // ----- Renderers -----

  // The provider-selection screen leads with the Claude subscription as the
  // recommended default -- a logo, headline, and full-width primary button --
  // and tucks the Anthropic Console and API-key paths behind a collapsed
  // "Other ways to sign in" disclosure so they don't compete for attention.
  function renderProviderSelection(): m.Vnode {
    return m("div.claude-login-select", [
      m("div.claude-login-primary", [
        claudeLogoIcon(),
        m("h3.claude-login-primary-headline", "Sign in with your Claude subscription"),
        m(
          "p.claude-login-primary-sub",
          "Connect your Claude.ai account to use your Pro or Max plan quota in this mind.",
        ),
        m(
          "button.claude-login-button.claude-login-button--primary.claude-login-button--block",
          { type: "button", onclick: () => void startOAuth("claudeai") },
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
              caretIcon(),
            ),
          ],
        ),
        alternativesExpanded
          ? m("div.claude-login-alts-list", [
              m("button.claude-login-alt", { type: "button", onclick: () => void startOAuth("console") }, [
                m("span.claude-login-alt-text", [
                  m("span.claude-login-alt-name", "Anthropic Console"),
                  m("span.claude-login-alt-desc", "Sign in with an API-billing account from console.anthropic.com."),
                ]),
                m("span.claude-login-alt-go", chevronRightIcon()),
              ]),
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
                  m("span.claude-login-alt-go", chevronRightIcon()),
                ],
              ),
            ])
          : null,
      ]),
    ]);
  }

  function renderApiKeyForm(): m.Vnode[] {
    return [
      m("p.claude-login-lead", "Paste an Anthropic API key. It's saved to this mind's host env file."),
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
                void submitApiKey();
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

  function renderOAuthCodeEntry(): m.Vnode[] {
    return [
      m("p.claude-login-lead", "Approve access in your browser, then paste the code Claude gives you back here."),
      m("div.claude-login-step", [
        m("div.claude-login-step-label", [m("span.claude-login-step-num", "1"), "Open the sign-in page"]),
        m(
          "a.claude-login-button.claude-login-button--primary.claude-login-button--block.claude-login-button--link",
          {
            href: oauthUrl,
            target: "_blank",
            rel: "noopener noreferrer",
          },
          [m("span", "Open sign-in page"), externalLinkIcon()],
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
        m("label.claude-login-step-label", { for: "claude-login-code-input" }, [
          m("span.claude-login-step-num", "2"),
          "Paste your code",
        ]),
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
              void submitOAuthCode();
            }
          },
        }),
        m(
          "p.claude-login-helper",
          "Claude shows a CODE#STATE string after you approve. Paste the whole thing, including the part after the #.",
        ),
      ]),
    ];
  }

  function renderStatus(kind: "loading" | "success" | "error", title: string, detail: string | null): m.Vnode {
    const icon = kind === "loading" ? spinnerIcon() : kind === "success" ? checkIcon() : warningIcon();
    return m("div.claude-login-status", [
      m(`div.claude-login-status-icon.claude-login-status-icon--${kind}`, icon),
      m("p.claude-login-status-title", title),
      detail !== null ? m("p.claude-login-status-detail", detail) : null,
    ]);
  }

  function renderSuccess(): m.Vnode {
    const status = successStatus;
    const email = status?.email ?? null;
    const tier = status?.subscription_type ?? null;
    let detail: string;
    if (email && tier) {
      detail = `Signed in as ${email}. Plan: ${tier}.`;
    } else if (email) {
      detail = `Signed in as ${email} via Anthropic Console.`;
    } else {
      detail = "You're signed in.";
    }
    return renderStatus("success", "All set", detail);
  }

  function renderInlineError(): m.Vnode {
    return m("div.claude-login-error-callout", [warningIcon(true), m("span", errorMessage ?? "")]);
  }

  // ----- Layout (header / body / footer) -----

  function titleForMode(): string {
    if (mode === "success") return "Signed in";
    if (mode === "error") return "Something went wrong";
    if (mode === "verifying") return "Just a moment";
    if (mode === "api_key_form") return "Sign in with API key";
    if (mode === "awaiting_oauth_code") return "Finish signing in";
    return "Sign in to Claude";
  }

  function renderBody(): m.Vnode | m.Vnode[] {
    if (mode === "success") return renderSuccess();
    if (mode === "error") {
      return renderStatus("error", "Couldn't complete sign-in", errorMessage ?? "An unexpected error occurred.");
    }
    if (mode === "verifying") return renderStatus("loading", verifyingTitle, verifyingDetail);
    if (mode === "awaiting_oauth_code") return renderOAuthCodeEntry();
    if (mode === "api_key_form") return renderApiKeyForm();
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
      return m("div.claude-login-footer.claude-login-footer--spread", [
        m(
          "button.claude-login-button.claude-login-button--ghost",
          { type: "button", onclick: () => attrsRef?.onDismiss() },
          "Close",
        ),
        m(
          "button.claude-login-button.claude-login-button--primary",
          { type: "button", onclick: () => goBackToProviderSelection() },
          "Try again",
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
              void submitApiKey();
            },
          },
          "Save & finish",
        ),
      ]);
    }
    // awaiting_oauth_code
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
          disabled: !code.trim(),
          onclick: () => {
            void submitOAuthCode();
          },
        },
        "Verify & finish",
      ),
    ]);
  }

  return {
    oncreate(vnode: m.VnodeDOM<ClaudeLoginModalAttrs>) {
      attrsRef = vnode.attrs;
    },

    onupdate(vnode: m.VnodeDOM<ClaudeLoginModalAttrs>) {
      attrsRef = vnode.attrs;
    },

    onremove() {
      abortOAuthIfActive();
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
              m("button.claude-login-close", { type: "button", onclick: onClose, "aria-label": "Close" }, closeIcon()),
            ]),
            m(
              "div.claude-login-body",
              mode === "awaiting_oauth_code" || mode === "api_key_form"
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
