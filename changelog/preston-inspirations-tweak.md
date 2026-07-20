- The publish skill's push step now states the commit structure as an explicit
  hard requirement: the published `main` must have **at least two commits** --
  at least one for the template files exactly as they came (the whole preserved
  history preferred, carried by parenting on `BASE_REF`) and **exactly one** on
  top for the inspiration's changes (all cleanups applied atomically so no
  pre-cleanup state exists as its own commit). This makes the existing
  `commit-tree -p BASE_REF` + `rev-list --count > 1` / `merge-base` mechanism's
  intent unmissable.

- The publish skill now handles a **GitHub push-protection rejection that names
  the Minds Google OAuth client** (`GOCSPX-...` / `...apps.googleusercontent.com`
  under `vendor/mngr`) as an expected, safe case: it is the shared
  Minds-provided sign-in client baked into the template, not the user's own
  secret or data. The agent explains this plainly and tells the user it is okay
  to approve the secret via GitHub's bypass link and retry the push -- never
  stripping it or treating the publish as failed.
