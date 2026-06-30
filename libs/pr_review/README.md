# pr-review

A code-aware web interface for reviewing your GitHub pull requests, served at
`/service/pr-review/` (port 8081).

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
   tree (ripgrep), and get type-aware hover / go-to-definition for Python (Jedi).
3. **Write-back.** Post general comments, submit line-comment reviews
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
- `src/pr_review/pyintel.py` -- Jedi-backed hover and go-to-definition.
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
Jedi, the path-traversal guards) runs for real against trees built in `tmp_path`.
