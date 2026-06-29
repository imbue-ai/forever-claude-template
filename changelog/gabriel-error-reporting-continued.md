- Added an `assist` skill (`/assist <description>`), invoked by the minds "get
  help -> have an agent help" flow. It diagnoses the problem, fixes what it can
  (user code, template built-in code, and `vendor/mngr` changes that affect this
  container), and reports built-in-code issues to imbue -- by POSTing its
  diagnosis to the minds report route, which opens a pre-filled "report a bug"
  modal for the user to review and submit. Issues that need a new desktop-app
  build (`apps/minds`, `mngr_forward`, `mngr_latchkey`, the outer vendored mngr)
  are reported but not fixed in place.

- The `update-self` skill now pulls with `--no-ff` and a recognizable
  `update-self:` merge-commit subject, so template (built-in) code can be
  identified from git history (used by the `assist` skill's built-in-vs-user
  classification).
