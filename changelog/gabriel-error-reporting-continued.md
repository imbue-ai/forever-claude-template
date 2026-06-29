- Added an `assist` skill (`/assist <description>`), invoked by the minds "get
  help -> have an agent help" flow. It reproduces and diagnoses the problem,
  confirms the diagnosis and proposed change with the user before editing
  anything, then fixes what it can (user code, template built-in code, and
  `vendor/mngr` changes that affect this container) and reports built-in-code
  issues to imbue -- by POSTing its diagnosis to the minds report route, which
  opens a pre-filled "report a bug" modal for the user to review and submit.
  Issues that need a new desktop-app build (`apps/minds`, `mngr_forward`,
  `mngr_latchkey`, the outer vendored mngr) are reported but not fixed in place.

- The `assist` skill applies fixes via the matching lifecycle path rather than
  hand-editing in place: a `apps/system_interface` (workspace UI) fix is routed
  through the `update-system-interface` skill (preview + safe reveal) and is
  never edited directly, since the `/assist` chat shares the served checkout;
  skill/service fixes are made live to unblock the user and then hardened in the
  background via `heal-artifact` / `edit-services`. It also holds its stated
  confidence to the evidence -- a cause is a hypothesis until the reported
  symptom is reproduced and tied to it, not "confirmed" from code-reading alone.

- The `update-self` skill now pulls with `--no-ff` and a recognizable
  `update-self:` merge-commit subject, so template (built-in) code can be
  identified from git history (used by the `assist` skill's built-in-vs-user
  classification).
