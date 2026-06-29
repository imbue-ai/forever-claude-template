# Changelog

## [Unreleased]

### Changed
- Step lifecycle commands now echo their decoration on stdout so a downstream progress view can read titles and summaries straight from the transcript (no side-channel file watching). `tk create --step` prints `Created <id>: <title>` (regular creates still print the bare id, so `ID=$(tk create ...)` captures are unaffected); `tk start <step>` prints `tk-step <id> title: <title>`; `tk close <step> "summary"` prints `tk-step <id> title: <title>` and `tk-step <id> summary: <summary>`.
- Removed step auto-nesting under an in_progress ticket (and the `--no-parent` flag that disabled it). `--parent` still works for explicitly nesting any ticket or step.
- Step records (`tk create --step`) now get an id with a literal `-step-` segment (e.g. `cod-step-f1zl`) instead of the plain `cod-f1zl`, so a step is distinguishable from a regular ticket by its id alone -- without reading the file's `step: true` frontmatter. Regular ticket ids are unchanged. (Lets a downstream progress view keep a step's grouping after its `.tickets/` file is deleted, when the frontmatter is no longer readable.)
- `tk close <id> "summary"` now stores the close summary in a dedicated, untimestamped `## Summary` section (printf-appended, so arbitrary text is escaping-safe) instead of a timestamped `## Notes` entry; a re-close replaces the prior summary rather than appending a duplicate
- Extracted `edit`, `ls`, `query`, and `migrate-beads` commands to plugins (ticket-extras)
- Timestamps (`created`, note timestamps) now carry microsecond precision where the platform's `date` supports it (GNU `%N`), with a `.000000` fallback elsewhere; the fixed-width format keeps lexicographic order equal to chronological order

### Added
- `tk start` stamps a `started:` frontmatter timestamp and `tk close` stamps a `closed:` timestamp, so consumers can order tickets by when work began and completed (used by downstream progress views)
- Plugin system: executables named `tk-<cmd>` or `ticket-<cmd>` in PATH are invoked automatically
- `super` command to bypass plugins and run built-in commands directly
- `TICKETS_DIR` and `TK_SCRIPT` environment variables exported for plugins
- `help` command lists installed plugins with descriptions
- Plugin metadata: `# tk-plugin:` comment for scripts, `--tk-describe` flag for binaries
- Multi-package distribution: `ticket-core`, `ticket-extras`, and individual plugin packages
- CI scripts for publishing to Homebrew tap and AUR

### Plugins
- ticket-edit 1.0.0: Open ticket in $EDITOR (extracted from core)
- ticket-ls 1.0.0: List tickets with optional filters (extracted from core); `ticket-list` symlink for alias
- ticket-query 1.0.0: Output tickets as JSON, optionally filtered with jq (extracted from core)
- ticket-migrate-beads 1.0.0: Import tickets from .beads/issues.jsonl (extracted from core)

## [0.3.2] - 2026-02-03

### Fixed
- Ticket ID lookup now trims leading/trailing whitespace (fixes issue with AI agents passing extra spaces)

## [0.3.1] - 2026-01-28

### Added
- `list` command alias for `ls`
- `TICKET_PAGER` environment variable for `show` command (only when stdout is a TTY; falls back to `PAGER`)

### Changed
- Walk parent directories to find `.tickets/` directory, enabling commands from any subdirectory
- Ticket ID suffix now uses full alphanumeric (a-z0-9) instead of hex for increased entropy

### Fixed
- `dep` command now resolves partial IDs for the dependency argument
- `undep` command now resolves partial IDs and validates dependency exists
- `unlink` command now resolves partial IDs for both arguments
- `create --parent` now validates and resolves parent ticket ID
- `generate_id` now uses 3-char prefix for single-segment directory names (e.g., "plan" → "pla" instead of "p")

## [0.3.0] - 2026-01-18

### Added
- Support `TICKETS_DIR` environment variable for custom tickets directory location
- `dep cycle` command to detect dependency cycles in open tickets
- `add-note` command for appending timestamped notes to tickets
- `-a, --assignee` filter flag for `ls`, `ready`, `blocked`, and `closed` commands
- `--tags` flag for `create` command to add comma-separated tags
- `-T, --tag` filter flag for `ls`, `ready`, `blocked`, and `closed` commands

## [0.2.0] - 2026-01-04

### Added
- `--parent` flag for `create` command to set parent ticket
- `link`/`unlink` commands for symmetric ticket relationships
- `show` command displays parent title and linked tickets
- `migrate-beads` now imports parent-child and related dependencies

## [0.1.1] - 2026-01-02

### Fixed
- `edit` command no longer hangs when run in non-TTY environments

## [0.1.0] - 2026-01-02

Initial release.
