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
  plus a bootable-base pre-check covering `pyproject.toml`,
  `supervisord.conf`, and the `/welcome` takeover markers; no upstream fetch,
  provenance link only), overlay only the selected paths, hard-failing secret
  scan scoped to the overlaid content, manifest + thumbnail generation,
  `/welcome` stable-region rewrite, side-effect-free boot smoke-check, single
  commit -- then fleshes out every manifest FILL-IN section with real prose
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
  welcome skill's inspiration region takes over the whole welcome (a custom
  message naming the inspiration instead of the generic template greeting),
  reads the manifest in the same turn, and asks the user how they want to
  adapt it. The generated manifest is a thorough, self-sufficient explainer
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
