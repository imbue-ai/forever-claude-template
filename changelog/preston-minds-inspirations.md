- Added **inspirations**: a publishable, reusable, bootable snapshot of the
  apps and features a mind has built. A mind can publish an inspiration as its
  own clean GitHub repo, and another mind can adapt one into itself. A single
  repo can accumulate several inspirations over time (one `inspiration-<name>.md`
  manifest per inspiration at the repo root). All interaction happens inline in
  chat -- there are no popups (an earlier iteration shipped a system_interface
  publish popup and GitHub-login modal; both were removed after live testing
  surfaced repeated popup-delivery failures and UX friction, and no
  system_interface changes remain in the final design).

- New **`/publish-inspiration`** skill. The lead asks in chat what to include
  (no user data by default), then dispatches ONE `launch-task` worker cycle.
  On its isolated worktree the worker runs `build_inspiration.sh` -- reset to
  the clean FCT base the mind was created from (first-parent-root fallback
  plus a bootable-base pre-check covering `pyproject.toml` and
  `supervisord.conf`; no upstream fetch, provenance link only), overlay only
  the selected paths, hard-failing secret scan scoped to the overlaid
  content, manifest + thumbnail generation, an inspiration-specific
  `/welcome` skill written into the snapshot, side-effect-free boot
  smoke-check, single commit -- then fleshes out every manifest FILL-IN section with real prose
  and replaces the placeholder thumbnail with a **bespoke, app-relevant SVG**
  (mock data only) before reporting done. Deterministic grep gates block
  publishing while any FILL-IN block or the placeholder-thumbnail marker
  remains, and an SVG-safety check rejects scripts/event handlers/foreignObject.

- **No merge-back, ever**: the lead confirms in chat, then pushes directly
  from the worker's worktree. Nothing merges into or writes to the live
  mind's checkout after assembly starts (an earlier iteration merged the
  assembly branch into the live checkout, which once reset a live mind's
  whole tree to an old base -- 1400+ files; the invariant is documented
  prominently in the skill).

- Publish confirmation is **inline in chat**: the lead presents the proposed
  title, description, repo name, and visibility (private default) once, takes
  edits in replies, commits any confirmed changes in the worker's worktree,
  and only then creates the repo. GitHub auth, when needed, is the **`gh`
  device flow surfaced in chat** (one-time code + github.com/login/device
  link, `workflow` scope requested up front, `GH_TOKEN`/`GITHUB_TOKEN`
  scrubbed from every `gh` invocation so the credential persists to gh's
  store, `gh auth setup-git` afterward) -- no agent restart. After a
  successful push the repo is tagged with the `minds-inspiration` GitHub
  topic and its description is set, so published inspirations are
  discoverable as a group.

- New **`/use-inspiration`** skill. Brings an existing inspiration into the
  current mind -- either as the template a new mind is created from, or by
  merging one in from a git URL (`git fetch` + `git merge
  --allow-unrelated-histories`; conflicts surfaced to the user as holes in
  plain language) -- then fills in the inspiration's holes interactively and
  appends a dated adaptation record to the manifest.

- A mind created from an inspiration repo starts adapting immediately: the
  published repo ships its own generated `/welcome` skill (a custom greeting
  naming the inspiration instead of the generic template greeting), which
  reads the manifest in the same turn and asks the user how they want to
  adapt it. The template's own welcome skill is untouched by this feature --
  the publish flow changes the welcome only inside the published snapshot. The generated manifest is a thorough, self-sufficient explainer
  (what it is, how it works, how to adapt it, holes, permissions, adaptation
  history).

- Added a one-sentence note in `CLAUDE.md` that inspirations exist. Publishing
  is user-initiated; the agent does not proactively push the user to create
  one.

- Fixed the publish push for git worktrees: `gh repo create --source=.` errors
  inside a worktree (its `.git` is a file, not a directory), which a real
  publish run hit. The skill now publishes in two steps -- create the empty
  repo, then push the assembled `mngr/<slug>` branch as `main` directly from
  the worktree (same full bootable tree) -- and cleans up the `inspiration`
  remote on close-out, since remotes live in the shared repo config and would
  otherwise linger in the live checkout and collide with the next publish.

- Fixed first boot hanging forever on "Loading workspace" for minds created
  from a private inspiration repo. Bootstrap's best-effort runtime-worktree
  fetch ran git without disabling terminal prompts, so against a private
  origin with no `GH_TOKEN` git prompted for a username on the tmux TTY and
  blocked bootstrap before supervisord ever started (the public template repo
  never triggered this, since anonymous fetches fail fast there). All
  bootstrap and runtime-backup git invocations now run with
  `GIT_TERMINAL_PROMPT=0`, turning any credential prompt into the fast,
  already-handled failure the best-effort design intended.

- Prerequisites are now a first-class, actionable manifest section. A real
  adoption got stuck because the adopting agent mentioned a needed Slack
  permission but never initiated it. The manifest's "Permissions it may need"
  prose section is replaced by "Prerequisites" -- machine-readable
  `requires_permission: <scope> / <schema>` and `requires_secret:` lines that
  state plainly the adopting agent must initiate each one itself (via a
  latchkey permission request) during setup. The use-inspiration flow is now
  activation-first: if the user keeps the same connectors, the agent sends the
  permission requests, wires secrets, and gets the app showing the user's OWN
  data -- the explicit definition of done for a data-backed app (a running
  service or a 200 response is not done) -- and invites them to try it BEFORE
  asking how they want to adapt it. The generated welcome ends its first
  response on the connect-your-accounts question instead of the adaptation
  question; "Holes" is now strictly the adaptation agenda.

- GitHub access for publishing now goes through latchkey permissioning like
  every other connector -- the `gh` CLI is banned from the flow entirely. The
  agent probes `latchkey curl https://api.github.com/user`, initiates a
  `github-rest-api` / `github-write-repos` permission request itself when
  needed (approved by the user in the minds app), creates the repo with the
  description and visibility in one API call, and sets the
  `minds-inspiration` topic via the API. The git push itself authenticates
  with the mind's standard `GH_TOKEN` (latchkey deliberately never hands raw
  tokens to the container, and a push is not an HTTP call it can inject
  into); if `GH_TOKEN` is absent the agent says so and stops rather than
  improvising. No named remote is written, so no cleanup is needed.

- The chat confirmation now embeds the designed thumbnail as a markdown image
  (the SVG's absolute worktree path), so the user sees exactly what will
  represent their inspiration while confirming the title, description, repo
  name, and visibility.
