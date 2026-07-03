- Added **inspirations**: a publishable, reusable snapshot of the apps and
  features a mind has built. A mind can publish an inspiration as its own clean
  GitHub repo, and another mind can adapt one into itself. A single repo can
  accumulate several inspirations over time (one `inspiration-<name>.md`
  manifest per inspiration at the repo root).

- New **`/publish-inspiration`** skill. It asks what to include (no user data by
  default), then assembles the snapshot in an isolated local `git worktree` in
  the same container: it resets to the clean FCT version the mind was based on
  (no upstream fetch -- provenance link only), overlays only the selected paths,
  runs a hard-failing secret scan (aborts before commit on any
  token/credential), generates the manifest + a placeholder SVG thumbnail,
  rewrites the `/welcome` stable region, and does a side-effect-free boot check.
  The user then confirms/edits the title, description, repository name,
  visibility, and thumbnail in a popup before the skill creates the repo and
  pushes.

- New **`/use-inspiration`** skill. Brings an existing inspiration into the
  current mind -- either as the template a new mind is created from (the rewritten
  `/welcome` drives it on startup), or by merging one in from a git URL -- then
  fills in the inspiration's "holes" interactively with the user and records what
  was adapted back into the manifest.

- New **system_interface publish popup**: a box in the workspace UI (backed by
  `/api/inspiration/*`) that previews the proposed inspiration and lets the user
  edit the fields before publishing. The SVG thumbnail is sanitized before it is
  previewed or committed.

- New **system_interface GitHub-login modal** (backed by `/api/github-auth/*`):
  a one-click GitHub login (web/device flow or a pasted token) for users without
  an in-VM `GH_TOKEN`, so publishing can push. It configures gh's credential
  store and the git credential helper in place -- no agent restart is needed
  (unlike the Claude API-key flow, the credential is only needed at `git push`
  time). All new credential/inspiration endpoints are restricted to loopback
  callers.

- Added a one-sentence note in `CLAUDE.md` that inspirations exist. Publishing
  is user-initiated; the agent does not proactively push the user to create one.

- Made inspiration assembly fast and reliable. The secret scan in
  `build_inspiration.sh` now scans only the paths overlaid out of the live mind
  (the selected apps/data plus carried-forward manifests) instead of the whole
  assembled tree; the clean FCT base is trusted and public, so scanning it only
  produced false positives on its own test-fixture placeholder tokens (e.g.
  `sk-ant-test`) that blocked every publish. The token patterns now also require
  a realistic key length after each prefix, so short placeholder values do not
  fire, and the single-pass scan no longer traverses `vendor/mngr` or the base's
  fixtures. The boot smoke-check now runs on the interpreter that already ships
  the supervisor library (the installed `supervisord` binary's shebang) rather
  than `uv run`, which had to resolve and build the entire project environment
  just to parse `supervisord.conf` -- slow on a cold base and prone to spurious
  aborts on unrelated build errors. Assembly now runs directly in a local
  throwaway `git worktree` in the same container instead of a `launch-task`
  sub-agent, which added minutes of latency for a sub-second job without adding
  isolation.

- Fixed the in-mind GitHub-login modal so it can actually persist a credential.
  The system_interface process inherits `GH_TOKEN` from the agent environment,
  and `gh` prioritizes `GH_TOKEN` / `GITHUB_TOKEN` (and enterprise variants) over
  its credential store -- so `gh auth login` refused to store the pasted/web
  credential and `gh auth status` reported the env token, and the modal never
  wrote anything durable. Every `gh` call in the GitHub-auth backend now runs
  with those variables scrubbed from the child environment (the parent process
  environment is untouched), and the publish skill scrubs them for its own
  `gh auth status` probe and final `gh repo create --push` too.

- Made the publish/GitHub-login popups reliable and fast to fall back from.
  Popup events were fire-and-forget over a transient WebSocket: if no live
  client was connected at broadcast time, the popup silently never appeared and
  the skill blind-polled for minutes. The backend now retains the pending
  publish request and any unresolved GitHub-auth prompt and replays them to
  every newly-connecting workspace client, and the trigger endpoints return a
  `ws_client_count` so the skill can skip waiting entirely when nobody is
  listening (one bounded ~90s wait otherwise, then an inline-chat / device-flow
  fallback -- no more serial popup re-triggering).

- Fixed base-commit resolution for multi-root mind repos. The publish skill's
  fallback now uses the first-parent root (never a bare root-commit lookup,
  which grabbed near-empty parallel roots from subtree merges), with a
  mandatory seconds-cheap pre-check that the resolved base tree is a bootable
  template (has `pyproject.toml` and `supervisord.conf`), walking forward along
  the first-parent chain when it is not; `build_inspiration.sh` re-validates
  and exits 5 with a clear message as a backstop.

- The GitHub-auth web login now requests the `workflow` scope (the template
  ships CI workflows, so pushing them requires it), the device-flow expect
  logic was rebuilt against gh 2.95's real PTY transcript (it no longer times
  out waiting for the one-time code), and the auth status surfaces the token's
  scopes with a warning when `workflow` is missing.

- A mind created from an inspiration repo now starts adapting immediately: the
  welcome skill's inspiration region takes over the whole welcome (a custom
  message naming the inspiration instead of the generic template greeting),
  reads the manifest in the same turn, and asks the user how they want to adapt
  it. The generated `inspiration-<slug>.md` manifest is now a thorough,
  self-sufficient explainer (what it is, how it works, how to adapt it, holes,
  permissions, adaptation history) with clearly-marked sections the publishing
  agent fleshes out before confirming.

- The confirmed thumbnail/manifest edits are committed before the push (no more
  placeholder-then-re-push), with a clean-git-status pre-push check.

- **Fixed a data-loss bug**: publishing could reset the live mind's own branch
  to an old inspiration base. The assembly worktree's commit has the live
  mind's HEAD as its parent but a tree fully reset to a (possibly much older)
  base; merging that branch into the live mind's checkout with `git merge`
  made git treat everything the old base lacked as an intentional deletion,
  wiping real content (in one incident, `vendor/mngr/src` and other live
  files). The publish skill no longer merges anything into the live mind: the
  assembly worktree's tree is pushed directly as the new repo, and the live
  checkout is never written to again after assembly starts. The base-ref
  pre-check also now requires the `/welcome` takeover markers, so a stale base
  is rejected before assembly rather than silently degrading later.

- Fixed silent 500s from the GitHub-login popup. A failed device-flow spawn or
  status check raised `GitHubAuthError`, which the backend turned into an HTTP
  error response without ever logging it -- so a failure was undiagnosable
  from the container's logs. Failures are now logged server-side, and the
  modal surfaces the real error detail (via the same error-parsing helper
  other views use) instead of a generic "Failed to start GitHub login" string.

- Hardened the publish skill against publishing a non-bootable "inspiration."
  A mind that hit the (now-fixed) destructive-merge bug on an older skill
  version worked around it by pushing just the app code plus a README directly
  via the GitHub API, bypassing the documented flow entirely -- producing a
  repo that `/use-inspiration`'s template path cannot boot from. Added a
  prominent callout making explicit that a valid inspiration is always the
  full assembled tree (the clean FCT base plus the selected paths, never a
  hand-picked subset), and that any failure in assembly, the popup/auth, or
  the push must be fixed-and-retried or reported to the user -- never worked
  around with an ad-hoc alternate publish mechanism.

- Fixed the generated manifest's "Holes" and "Permissions it may need"
  sections being left empty even when the included app clearly needed one (a
  Slack permission, in one case). The manifest template already generated
  `FILL-IN` placeholder comments for the publishing agent to complete, but the
  skill never instructed anyone to actually go do that, so the placeholders
  routinely shipped as-is. Added an explicit, mandatory step right after
  assembly to flesh out every `FILL-IN` block with real content (or an
  explicit "none"), plus a hard gate the skill must run before opening the
  publish popup that greps for any remaining placeholder and blocks
  publishing until it is gone -- matching this skill's existing
  deterministic-gate pattern (the secret scan) rather than relying on prose
  alone. The assembly script's closing summary also reminds the agent inline.

- Final audit pass over the whole feature: a fresh, skeptical read of every
  skill and backend/frontend file confirmed the five rounds of fixes above
  compose correctly (no dangling cross-references, no contract mismatches
  between the WS events / `ws_client_count` / endpoint paths on either side),
  fixed one stale comment in `build_inspiration.sh` still describing the old
  launch-task-sub-agent execution model, and confirmed the full system_interface
  test suite (540 tests) and frontend suite (384 tests + build) pass clean.
  Separately confirmed `libs/mngr` needs no change: its worktree branches are
  always real descendants of their base (it never resets a worktree's tree to
  an unrelated older commit the way the destructive-merge bug required), and it
  has no `gh repo create`-style primitive the FCT skill could have reused.
