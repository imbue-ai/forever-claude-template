# pr-review

A code-aware web interface for reviewing your GitHub pull requests, served at
`/service/pr-review/` (port 8082).

## What it does

1. **PR list.** Lists the authenticated viewer's open PRs (authored +
   review-requested) with status signals -- CI verdict, review decision,
   merge-conflict state, and diffstat -- enriched lazily per row. A left status
   strip (with a legend and per-row tooltip) summarizes each PR at a glance, and
   a toolbar offers repo grouping (or a flat list), sorting, and a title/repo
   search; the filter / grouping / sort choices persist in `localStorage`.
2. **Code-aware diff view.** On opening a PR it fetches the full repo source at
   the PR head commit (GitHub tarball) and caches it under
   `runtime/pr-review/repos/`, then renders changed files as full-file diffs in a
   Monaco editor. You can open any file in the repo, find-usages across the whole
   tree (ripgrep), and get code-aware hover / go-to-definition for Python (Jedi)
   and for JavaScript / TypeScript (tree-sitter -- `.js/.jsx/.ts/.tsx` and their
   `.mjs/.cjs/.mts/.cts` variants).
3. **Rich types (opt-in, per repo).** tree-sitter is a parser, so JS/TS hover
   shows declarations but not inferred types or third-party members (e.g.
   `session.fromPartition` from `require('electron')`). The "Types: basic /
   Enable rich" pill in the detail header opts a repo into real type resolution:
   it launches a headless agent (`claude -p`) inside the cached tree that
   installs the repo's dependencies (npm / pnpm / ...) and a pinned TypeScript
   5.x language server (isolated under `.pr-review-prep/`), after which JS/TS
   hover / go-to-definition come from that language service (member + inferred
   types, library `.d.ts`), falling back to tree-sitter on any error. State lives
   under `.pr-review-prep/` in the tree; "clear" removes it and the installed
   `node_modules` for that checkout (the shared store is left intact). A finished
   prep is shared across PRs and pushes: it is keyed by a fingerprint of the
   repo's dependency files (`package.json` + lockfiles) rather than the commit
   SHA and published to a store under `runtime/pr-review/prep/`, so any later
   checkout whose dependencies match reuses it by symlink instead of
   reinstalling. When the dependencies match an existing prep exactly, rich types
   **auto-enable** silently (no agent) -- the pill flips to "rich" on its own.
   Only a genuine install (new or changed dependencies) launches the agent, which
   runs the packages' install scripts and spends Claude usage; that remains
   strictly manual behind the Enable action, and it is seeded from the repo's
   nearest prior prep so it updates incrementally rather than from scratch. Note:
   `npm install typescript` now resolves to TypeScript 7.x, whose npm package
   lacks the classic language service API -- the agent pins `typescript@5` for
   this reason.
4. **Write-back.** Post general comments, submit line-comment reviews
   (comment / approve / request-changes), edit the PR title/description, and
   close / reopen or merge a PR (merge / squash / rebase) -- from the detail
   page or a home-page right-click menu, behind a confirm step. Marking a draft
   "ready for review" is not offered: GitHub exposes that only through its
   GraphQL API, which this tool's REST-only credentialed access cannot reach.

## How GitHub access works

Every GitHub call goes through `latchkey curl`, so the user's stored credentials
are injected transparently and no token ever lives in this process. The transport
is a single seam (`github._curl`); each network function takes an injectable
`curl` parameter that defaults to it, which is how the tests run without touching
the network.

The CI verdict deliberately ignores GitHub's legacy combined-status endpoint when
it reports zero statuses: that endpoint defaults to `pending` with no statuses,
which would otherwise wrongly override a clean check-runs result.

## Layout

- `src/pr_review/runner.py` -- the Flask app and routes.
- `src/pr_review/github.py` -- GitHub access, status enrichment, the repo-tree
  cache, and ripgrep find-usages.
- `src/pr_review/pyintel.py` -- Jedi-backed hover and go-to-definition (Python).
- `src/pr_review/jsintel.py` -- tree-sitter-backed hover and go-to-definition
  (JavaScript / TypeScript): declaration signatures + doc comments, and
  definitions resolved locally and across relative imports in the cached tree.
- `src/pr_review/prepare.py` -- the opt-in "rich types" state machine: launches
  the headless install/setup agent, tracks state under `.pr-review-prep/`, and
  shares finished preps across checkouts via a dependency-fingerprint-keyed store
  under `runtime/pr-review/prep/` (reuse by symlink, auto-enable, seeded installs).
- `src/pr_review/tsintel.py` -- rich hover / go-to-definition via a persistent
  TypeScript language service, used for prepared repos (falls back to jsintel).
- `src/pr_review/assets/tsintel_server.mjs` -- the Node language-service helper
  that `tsintel.py` drives (line-delimited JSON protocol).
- `src/pr_review/assets/` -- the frontend (`index.html`, `app.js`, `app.css`);
  Monaco loads from a CDN and all fetches are relative so the app works behind
  the system_interface proxy.
- `src/pr_review/testing.py` -- test helpers (`FakeCurl` and friends).

## Testing

```
cd libs/pr_review && uv run pytest
```

Tests never make real network calls, real `latchkey` calls, or real writes: the
`curl` transport is injected as a `FakeCurl`, and repo-tree-backed routes are
served from a pre-seeded on-disk cache. On-disk behavior (the cache, ripgrep,
Jedi, tree-sitter, the path-traversal guards) runs for real against trees built
in `tmp_path`. The rich-types paths are seam-injected too -- the prepare agent
launcher and the `tsintel` language-service process are never spawned for real in
the suite (the `claude -p` agent + Node language service are exercised by hand /
in the release check).
