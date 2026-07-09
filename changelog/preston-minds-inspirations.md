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
  and only then creates the repo. After a successful push the repo is tagged
  with the `minds-inspiration` GitHub topic and its description is set, so
  published inspirations are discoverable as a group. (GitHub auth went
  through two earlier iterations -- a system_interface login modal, then a
  chat-surfaced `gh` device flow -- before landing on latchkey permissioning
  end-to-end; see the latchkey bullet below.)

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
  the worktree (same full bootable tree). (An interim version added a named
  `inspiration` remote and cleaned it up on close-out; the final flow writes
  no named remote at all, so there is nothing to clean up.)

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

- GitHub access for publishing now goes through latchkey permissioning
  end-to-end -- the `gh` CLI is banned from the flow entirely, and no GitHub
  token ever enters the container. The agent probes access and initiates the
  permission requests itself when needed (approved by the user in the minds
  app): `github-rest-api` (`github-read-user` + `github-write-all` -- repo
  creation is `POST /user/repos`, which the narrower `github-write-repos`
  path schema does not cover) for creating the repo (one API call carrying
  name, description, and visibility) and setting the `minds-inspiration`
  topic, plus `github-git` / `github-git-write` for the push. The access
  probes pass `-f`, since `latchkey curl` exits with curl's own code and the
  gateway's 403 denial would otherwise read as success. The latchkey gateway natively proxies GitHub's git smart-HTTP
  endpoints, so the push runs plain `git push` against the gateway's proxy
  URL (`$LATCHKEY_GATEWAY/gateway/https://github.com/...`) with the
  credential injected server-side -- an earlier iteration authenticated the
  push with the mind's `GH_TOKEN` on the mistaken assumption that a push was
  not something latchkey could carry. Permission-request bodies now use
  latchkey's required four-field format (`agent_id` / `type` / `payload` /
  `rationale`; the scope and permissions used to be sent at the top level).
  No named remote and no credential is ever written to disk or git config,
  so no cleanup is needed. (The matching gateway-side change -- raising the
  gateway's request-body cap so full-history push packfiles fit -- lives in
  the mngr repo's `mngr_latchkey` changelog.)

- The `latchkey` skill now documents the general git-over-gateway pattern
  (proxy URL + the two gateway auth headers, `github-git-read` /
  `github-git-write`), and `/use-inspiration` uses it to fetch private
  inspiration repos when the anonymous fetch fails.

- The chat confirmation now embeds the designed thumbnail as a markdown image
  (the SVG's absolute worktree path), so the user sees exactly what will
  represent their inspiration while confirming the title, description, repo
  name, and visibility.

- The post-assembly confirmation is now a hard gate too. A live publish ran
  the scope gate correctly, then -- after assembly -- verified the gates
  itself, announced "everything checks out," and pushed in the same turn:
  the user never saw the final title, description, or thumbnail before the
  repo existed on their account. The confirmation section now requires
  ending the turn after presenting the final details (thumbnail embedded)
  and proceeding to repo-creation + push only on an explicit reply to that
  message; it spells out that no earlier approval counts (scope
  confirmation, a pre-assembly "go ahead," or the GitHub permission
  approvals) and that the agent's own gate checks are verification, never
  confirmation.

- The setup Q&A now ends in a SCOPE gate, not just a name check. A live
  publish laid out its proposal and dispatched the assembly worker in the
  same turn, declaring the include set "confirmed" without any user reply.
  The gate now requires one plain-language message covering what will be
  included, what will NOT be (data, other apps, secrets/config), any
  published-version modifications, and the proposed (adjustable) name --
  and an explicit user reply to THAT message before any assembly work or
  dispatch; the skill spells out that confirmation is something the user
  gives, never something the agent declares.

- Published-version modifications are a first-class part of the flow: the
  user can ask for files to be changed, generalized, or stripped in the
  published snapshot only (a secret-cleaned copy, a removed personal
  preference) -- confirmed at the scope gate, carried in the worker task
  file, applied by the worker in its isolated worktree (the live mind's
  files and history are untouched), re-scanned with the same secret-token
  patterns the assembly script enforces, and recapped in the final chat
  confirmation.

- The published repo's HISTORY no longer contains the mind's own commits.
  `build_inspiration.sh` used to parent the snapshot commit on the mind's
  HEAD, which shipped the mind's entire commit history -- including anything
  ever committed and later removed (a "secret-cleaned" file's dirty original
  stayed retrievable from history). The snapshot commit is now parented on
  `BASE_REF` via `git commit-tree`, so the published history is exactly the
  public template's history plus the snapshot commits. Verified on a
  synthetic repo: a committed-then-removed secret in the mind is unreachable
  from the pushed branch.

- The name is confirmed BEFORE assembly starts, and a rename never restarts
  assembly. A live publish derived a title itself, dispatched the worker,
  and then tore the worker down and relaunched it when the user renamed the
  inspiration -- unnecessarily, since the worker's name and branch are
  internal plumbing that appear nowhere in the published repo. Setup now
  ends with a hard gate (the agent echoes the proposed title, repo name,
  scope, and data inclusion and waits for the go-ahead before dispatching),
  and the skill states explicitly that a post-dispatch rename is handled in
  place: pass the new slug to the build script if it has not run yet,
  otherwise `git mv` the slug-bearing files and fix the front-matter/welcome
  references in the worker's worktree (preserving completed FILL-IN prose
  and the bespoke SVG) -- never a teardown.

- `BASE_REF` resolution is now fully deterministic -- no judgment call. A live
  publish from a fresh mind surfaced the gap: with no `update-self:` commits,
  the documented fallback (first-parent root) pointed at an ancient template
  commit, and the publishing agent had to diverge by hand to the workspace's
  actual creation snapshot. The rule is now: the newest first-parent commit
  whose subject is a template-state marker -- `update-self: ...` or
  bootstrap's `Initial workspace commit` (always present: created
  `--allow-empty` at first boot, snapshotting exactly what the workspace
  started from, including any uncommitted source state a dev-flow clone
  carried). The first-parent root remains only as a last resort for repos
  with no marker at all.
