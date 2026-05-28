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
 * The modal is purely reactive: it opens when ChatPanel receives an
 * auth-error event over the SSE stream, and closes only when the user
 * dismisses it. It does not poll the backend status endpoint, because
 * `claude auth status` reflects the system-interface process's view of
 * auth which can disagree with the already-running agent's cached
 * in-process auth decision.
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

type Provider = "claudeai" | "console" | "api_key";
type Mode = "select_provider" | "api_key_form" | "awaiting_oauth_code" | "verifying" | "success" | "error";

export interface ClaudeLoginModalAttrs {
  chatAgentName: string | null;
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

function copyIcon(): m.Vnode {
  return m(
    "svg",
    {
      width: 13,
      height: 13,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    [
      m("rect", {
        x: 9,
        y: 9,
        width: 11,
        height: 11,
        rx: 2,
        stroke: "currentColor",
        "stroke-width": 2,
      }),
      m("path", {
        d: "M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1",
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
  // The OAuth provider chosen on the select screen. `submitOAuthCode`
  // reads it to tailor the verifying copy: the console flow restarts the
  // mind's claude agents (its credential lands in the cached .claude.json),
  // the subscription flow does not.
  let oauthProvider: "claudeai" | "console" | null = null;
  let sessionId: string | null = null;
  let oauthUrl: string | null = null;
  let code = "";
  let apiKey = "";
  let apiKeyRevealed = false;
  let urlCopied = false;
  let urlCopiedResetHandle: ReturnType<typeof setTimeout> | null = null;
  let errorMessage: string | null = null;
  let verifyingTitle = "Working...";
  let verifyingDetail: string | null = null;
  let successStatus: ClaudeAuthStatus | null = null;
  let attrsRef: ClaudeLoginModalAttrs | null = null;

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
    oauthProvider = chosen;
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
    startVerifying(
      "Verifying code...",
      oauthProvider === "console"
        ? "Completing sign-in and restarting this mind's Claude agents so the new credentials take effect."
        : "Completing the OAuth handshake.",
    );
    try {
      const status = await m.request<ClaudeAuthStatus>({
        method: "POST",
        url: apiUrl("/api/claude-auth/submit-code"),
        body: {
          session_id: sessionId,
          code: code.trim(),
          chat_agent_name: attrsRef?.chatAgentName ?? null,
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
    startVerifying(
      "Restarting Claude agents...",
      "Saving your API key and respawning every running claude in this mind so the new key takes effect.",
    );
    try {
      const status = await m.request<ClaudeAuthStatus>({
        method: "POST",
        url: apiUrl("/api/claude-auth/submit-api-key"),
        body: {
          api_key: apiKey.trim(),
          chat_agent_name: attrsRef?.chatAgentName ?? null,
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
      // Clipboard access can be denied (insecure context, permissions);
      // the URL stays visible and selectable, so silently skip feedback.
      return;
    }
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

  function renderProviderSelection(): m.Vnode {
    const providers: Array<{
      id: Provider;
      label: string;
      description: string;
    }> = [
      {
        id: "claudeai",
        label: "Claude subscription",
        description: "Sign in with your Claude.ai account. Uses your Pro / Max plan quota.",
      },
      {
        id: "console",
        label: "Anthropic Console",
        description: "Sign in with an API-billing account from console.anthropic.com.",
      },
      {
        id: "api_key",
        label: "Use an API key",
        description: "Paste a raw sk-ant-... key. Requires restarting every running claude.",
      },
    ];
    return m("div.claude-login-providers", [
      m("p.claude-login-intro", "Pick how you'd like to authenticate."),
      ...providers.map((p) =>
        m(
          "button.claude-login-provider",
          {
            type: "button",
            onclick: () => {
              if (p.id === "api_key") {
                mode = "api_key_form";
                m.redraw();
              } else {
                void startOAuth(p.id);
              }
            },
          },
          [m("div.claude-login-provider-label", p.label), m("div.claude-login-provider-description", p.description)],
        ),
      ),
    ]);
  }

  function renderApiKeyForm(): m.Vnode[] {
    return [
      m("div.claude-login-field", [
        m("label.claude-login-step-label", { for: "claude-login-api-key-input" }, [
          m("span.claude-login-step-num", "1"),
          "Paste your Anthropic API key",
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
        m(
          "p.claude-login-helper",
          "Saved to the mind's host env file. Every running claude process is restarted so the new key takes effect.",
        ),
      ]),
    ];
  }

  function renderOAuthCodeEntry(): m.Vnode[] {
    return [
      m("div.claude-login-step", [
        m("div.claude-login-step-label", [
          m("span.claude-login-step-num", "1"),
          "Open this URL in your browser and sign in",
        ]),
        m("div.claude-login-url-box", [
          m(
            "a.claude-login-url",
            {
              href: oauthUrl,
              target: "_blank",
              rel: "noopener noreferrer",
            },
            oauthUrl,
          ),
          m(
            "button.claude-login-url-action",
            {
              type: "button",
              onclick: () => {
                void copyOAuthUrl();
              },
              "aria-label": "Copy URL to clipboard",
            },
            [copyIcon(), urlCopied ? "Copied" : "Copy"],
          ),
        ]),
      ]),
      m("div.claude-login-step", [
        m("label.claude-login-step-label", { for: "claude-login-code-input" }, [
          m("span.claude-login-step-num", "2"),
          "Paste the code shown after sign-in",
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
          "Save and restart",
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
        "Verify",
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
