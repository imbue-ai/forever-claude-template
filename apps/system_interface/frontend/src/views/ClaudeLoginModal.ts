/**
 * Modal that walks the user through re-authenticating Claude inside a mind
 * when credentials didn't sync from the host. Three sign-in paths:
 *
 * - Claude subscription: drive `claude auth login --claudeai` via the PTY
 *   subprocess on the backend, parse the printed OAuth URL, paste back the
 *   user's CODE#STATE.
 * - Anthropic Console: same flow but `--console`.
 * - Raw API key: paste a `sk-ant-...` value; backend writes it to the host
 *   env file and restarts the chat agent.
 *
 * The success state reads `subscription_type` from `claude auth status
 * --json` and renders conditionally, since Console accounts have no
 * subscription tier.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export interface ClaudeAuthStatus {
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
type Mode = "select_provider" | "awaiting_oauth_code" | "verifying" | "success" | "error";

const POLL_INTERVAL_MS = 3000;

export interface ClaudeLoginModalAttrs {
  chatAgentName: string | null;
  // Called when the user accepts the success state (clicks "Done", or
  // clicks the close affordance while the modal is in the success mode).
  // Parent should drop both the modal and any not-signed-in banner.
  onDismiss: () => void;
  // Called when the user closes the modal without reaching success.
  // Parent should drop the modal and show the not-signed-in banner so
  // the user can re-open the flow later.
  onMinimize: () => void;
}

export function ClaudeLoginModal(): m.Component<ClaudeLoginModalAttrs> {
  let mode: Mode = "select_provider";
  let provider: Provider = "claudeai";
  let sessionId: string | null = null;
  let oauthUrl: string | null = null;
  let code = "";
  let apiKey = "";
  let errorMessage: string | null = null;
  let successStatus: ClaudeAuthStatus | null = null;
  let pollTimer: number | null = null;
  let attrsRef: ClaudeLoginModalAttrs | null = null;

  function clearError(): void {
    errorMessage = null;
  }

  function setError(message: string): void {
    errorMessage = message;
    mode = "error";
    m.redraw();
  }

  async function pollStatus(): Promise<void> {
    if (mode === "success" || mode === "verifying") {
      return;
    }
    try {
      const status = await m.request<ClaudeAuthStatus>({
        method: "GET",
        url: apiUrl("/api/claude-auth/status"),
      });
      if (status.logged_in) {
        successStatus = status;
        mode = "success";
        m.redraw();
        // Notify the backend so the welcome-resend chokepoint fires for
        // the poll-detected path too. The endpoint is idempotent (the
        // welcome resend skips when the agent's pane already shows the
        // greeting) so failures here are harmless to swallow.
        try {
          await m.request({
            method: "POST",
            url: apiUrl("/api/claude-auth/notify-success"),
            body: { chat_agent_name: attrsRef?.chatAgentName ?? null },
          });
        } catch {
          // Best-effort; the user can still re-run /welcome manually.
        }
      }
    } catch {
      // Silently ignore poll failures; the user can still drive the flow.
    }
  }

  function startPolling(): void {
    if (pollTimer !== null) return;
    pollTimer = window.setInterval(() => {
      void pollStatus();
    }, POLL_INTERVAL_MS);
  }

  function stopPolling(): void {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function startOAuth(chosen: Provider): Promise<void> {
    provider = chosen;
    clearError();
    mode = "verifying";
    m.redraw();
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
    mode = "verifying";
    m.redraw();
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
        setError("Authentication did not succeed. Please try again.");
      }
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setError(errResp?.detail ?? "Failed to verify code");
    }
  }

  async function submitApiKey(): Promise<void> {
    if (!apiKey.trim()) return;
    clearError();
    mode = "verifying";
    m.redraw();
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
        setError("Anthropic did not accept the API key. Double-check and try again.");
      }
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setError(errResp?.detail ?? "Failed to save API key");
    }
  }

  function renderProviderSelection(): m.Vnode {
    return m("div.claude-login-providers", [
      m("p.text-text-secondary", "How would you like to sign in?"),
      m("div.flex.flex-col.gap-2.mt-3", [
        renderProviderButton("claudeai", "Claude subscription", "Sign in with your Claude.ai account (recommended)."),
        renderProviderButton(
          "console",
          "Anthropic Console",
          "Sign in with API-usage billing via console.anthropic.com.",
        ),
        renderProviderButton("api_key", "Use an API key", "Paste a raw sk-ant-... key."),
      ]),
    ]);
  }

  function renderProviderButton(p: Provider, label: string, description: string): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        class:
          "claude-login-provider-button text-left p-3 border border-border rounded hover:bg-surface-hover focus:ring",
        onclick: () => {
          if (p === "api_key") {
            provider = p;
            mode = "select_provider";
            apiKey = "";
            m.redraw();
          } else {
            void startOAuth(p);
          }
        },
      },
      [m("div.font-semibold", label), m("div.text-sm.text-text-secondary", description)],
    );
  }

  function renderApiKeyForm(): m.Vnode {
    return m("div.claude-login-api-key", [
      m("label.block.font-semibold.mb-1", { for: "claude-login-api-key-input" }, "Anthropic API key"),
      m("input", {
        id: "claude-login-api-key-input",
        type: "password",
        class: "w-full p-2 border rounded",
        placeholder: "sk-ant-...",
        value: apiKey,
        oninput: (event: InputEvent) => {
          apiKey = (event.target as HTMLInputElement).value;
        },
      }),
      m("div.flex.gap-2.mt-3", [
        m(
          "button",
          {
            type: "button",
            class: "px-4 py-2 border rounded",
            onclick: () => {
              provider = "claudeai";
              apiKey = "";
              m.redraw();
            },
          },
          "Back",
        ),
        m(
          "button",
          {
            type: "button",
            class: "px-4 py-2 bg-primary text-white rounded",
            disabled: !apiKey.trim(),
            onclick: () => {
              void submitApiKey();
            },
          },
          "Save",
        ),
      ]),
    ]);
  }

  function renderOAuthCodeEntry(): m.Vnode {
    return m("div.claude-login-oauth", [
      m("p", "Open this URL in your browser, complete the sign-in, then paste the code back here."),
      m(
        "a.block.mt-2.break-all.text-link",
        { href: oauthUrl ?? "#", target: "_blank", rel: "noopener noreferrer" },
        oauthUrl,
      ),
      m("label.block.font-semibold.mt-3.mb-1", { for: "claude-login-code-input" }, "Code"),
      m("input", {
        id: "claude-login-code-input",
        type: "text",
        class: "w-full p-2 border rounded font-mono",
        placeholder: "CODE#STATE",
        value: code,
        oninput: (event: InputEvent) => {
          code = (event.target as HTMLInputElement).value;
        },
      }),
      m("div.flex.gap-2.mt-3", [
        m(
          "button",
          {
            type: "button",
            class: "px-4 py-2 border rounded",
            onclick: () => {
              sessionId = null;
              oauthUrl = null;
              code = "";
              mode = "select_provider";
              m.redraw();
              void m.request({ method: "POST", url: apiUrl("/api/claude-auth/abort") });
            },
          },
          "Back",
        ),
        m(
          "button",
          {
            type: "button",
            class: "px-4 py-2 bg-primary text-white rounded",
            disabled: !code.trim(),
            onclick: () => {
              void submitOAuthCode();
            },
          },
          "Verify",
        ),
      ]),
    ]);
  }

  function renderSuccess(): m.Vnode {
    const status = successStatus;
    const email = status?.email ?? null;
    const tier = status?.subscription_type ?? null;
    let line: string;
    if (email && tier) {
      line = `Signed in as ${email} — subscription: ${tier}`;
    } else if (email) {
      line = `Signed in as ${email} via Anthropic Console`;
    } else {
      line = "Signed in.";
    }
    return m("div.claude-login-success", [
      m("p.text-lg.font-semibold", line),
      m(
        "div.mt-3",
        m(
          "button",
          {
            type: "button",
            class: "px-4 py-2 bg-primary text-white rounded",
            onclick: () => attrsRef?.onDismiss(),
          },
          "Done",
        ),
      ),
    ]);
  }

  function renderError(): m.Vnode {
    return m("div.claude-login-error", [
      m("p.text-red-500", errorMessage ?? "An error occurred."),
      m(
        "div.mt-3",
        m(
          "button",
          {
            type: "button",
            class: "px-4 py-2 border rounded",
            onclick: () => {
              mode = "select_provider";
              clearError();
              m.redraw();
            },
          },
          "Try again",
        ),
      ),
    ]);
  }

  function renderBody(): m.Vnode {
    if (mode === "success") return renderSuccess();
    if (mode === "error") return renderError();
    if (mode === "verifying") return m("p", "Working...");
    if (mode === "awaiting_oauth_code") return renderOAuthCodeEntry();
    if (provider === "api_key" && mode === "select_provider") return renderApiKeyForm();
    return renderProviderSelection();
  }

  return {
    oncreate(vnode: m.VnodeDOM<ClaudeLoginModalAttrs>) {
      attrsRef = vnode.attrs;
      startPolling();
      void pollStatus();
    },

    onupdate(vnode: m.VnodeDOM<ClaudeLoginModalAttrs>) {
      attrsRef = vnode.attrs;
    },

    onremove() {
      stopPolling();
      void m.request({ method: "POST", url: apiUrl("/api/claude-auth/abort") });
    },

    view() {
      // Close-affordance routing: in `success` mode the user is signed
      // in, so close is treated as confirmation (drop the banner too).
      // In every other mode the user is still unauthenticated, so close
      // collapses to the recovery banner.
      const onClose = (): void => {
        if (mode === "success") {
          attrsRef?.onDismiss();
        } else {
          attrsRef?.onMinimize();
        }
      };
      return m(
        "div.claude-login-overlay",
        {
          style:
            "position: absolute; inset: 0; background: rgba(0,0,0,0.4); z-index: 50; display: flex; align-items: center; justify-content: center;",
        },
        m(
          "div.claude-login-modal",
          {
            style:
              "position: relative; background: var(--color-surface, white); padding: 24px; border-radius: 8px; max-width: 480px; width: 90%; box-shadow: 0 8px 24px rgba(0,0,0,0.2);",
          },
          [
            m(
              "button",
              {
                type: "button",
                class: "claude-login-dismiss",
                onclick: onClose,
                style:
                  "position: absolute; top: 8px; right: 12px; background: transparent; border: none; cursor: pointer; font-size: 1.2em; color: inherit;",
                "aria-label": "Close",
              },
              "\u00d7",
            ),
            m("h2.text-xl.font-bold.mb-3", "Sign in to Claude"),
            renderBody(),
          ],
        ),
      );
    },
  };
}
