- New **`update-version`** skill owns the workspace's version ledger end to end:
  the `VERSION_HISTORY.md` format, seeding the "created from" line, appending a
  `## Workspace` line when a template update lands, appending an
  `## Inspirations` entry when something is published (computing the next
  `v<n>` per slug), and the rules that keep a retried step from double-recording.
  Both writers reference the one skill, so there is no helper program to keep in
  sync.

- **`update-self`** now records the version it moved to as part of landing an
  update, so a workspace's template lineage is visible in its own git tree.

- The publish skill now confirms the **adopter's required permissions with the
  publisher**. The manifest's "Prerequisites" -- what the inspiration's user must
  grant for the app to work -- are surfaced back in the chat confirmation in
  plain language, and the publisher's answer is part of the go-ahead. A missing
  or wrong line is fixed before the push, since a gap there silently breaks
  adoption.

- **An inspiration can be anything committable**, not just an app: a skill, a
  chat customization or behavior, a workflow, a service, config, or seed data.
  If the user wants to snapshot something that is not committed to git -- an
  ephemeral chat behavior, conversation history, runtime-only state -- the skill
  recognizes this and suggests turning it into something committable first (most
  often by crystallizing it into a skill), since an inspiration must be
  reconstructable from the committed tree.

- **LLM access is now a first-class prerequisite.** Any inspiration whose code
  calls Claude records how it reaches it, because that differs per environment:
  the keyed path (`ANTHROPIC_API_KEY` set -> litellm, pay-per-token) or the
  keyless path (`claude -p` -> the subscription credit pool). The manifest gains
  a `requires_llm:` line naming the method the code was built against, so an
  adopter on the other method knows to switch the model calls, and a hardcoded
  path is also listed as a Hole.

- **Published inspiration repos are locked down on creation.** Right after the
  repo is created, discussions are turned off, unconditionally and without
  asking. Issues stay enabled so collaborators can still file them;
  private-by-default is what makes issues and PRs collaborators-only and keeps
  the repo unforkable by outsiders. This closes the surfaces where arbitrary,
  non-collaborator users could comment on someone's inspiration. Two limits are
  surfaced to the user if they choose public visibility: GitHub has no
  collaborators-only-issues setting for a public repo, and forking cannot be
  disabled on a personal public repo (GitHub only allows that on org-owned
  repos) -- keeping the inspiration private avoids both.

- **Published manifests now carry a version and a recipe.** Each
  `inspiration-<slug>.md` records `version: v1` and a "Recipe" section: the
  include paths, the deliberate exclusions, and the published-version
  modification RULES (rules only -- never the removed values). An inspiration is
  derived from its workspace by that recipe rather than being a fork of it, so a
  later update re-runs the recipe against the current workspace instead of
  diffing two repos -- which is what keeps anything deliberately excluded
  excluded, even though it still exists in the source workspace.

- A publish records its entry in the workspace's ledger only **after the push
  succeeds** -- one single-file commit, documented as the one explicit exception
  to the rule that the live workspace is untouched after assembly. An
  unpublished inspiration is never recorded.

- A published inspiration never ships version history at all: `VERSION_HISTORY.md`
  is a workspace artifact, so the assembled snapshot drops it entirely (rather
  than shipping an empty copy). The slugs, repo URLs, and source commits of a
  mind's other inspirations therefore never appear inside one it publishes, and a
  mind created from the inspiration grows its own ledger on demand.
