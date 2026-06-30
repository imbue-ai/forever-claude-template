- Added `.github/workflows/sync-vendor-mngr.yml`, a scheduled job that keeps
  `vendor/mngr` tracking mngr `main`. Twice daily (an hour before each
  `minds-launch-to-msg` cron) it clones mngr main, re-archives it into
  `vendor/mngr` (the same `git archive` flow as mngr's `just sync-vendor-mngr`),
  and pushes the refresh to `main`; it no-ops when mngr main has not moved.
  Previously `vendor/mngr` was only refreshed at release time and drifted
  hundreds of commits behind mngr main between releases, risking a vendor-skew
  wedge in the launch-to-msg health check (binary built from mngr main, agent
  created from this repo's stale committed vendor). mngr is public, so reading
  it needs no credential and the push uses the built-in `GITHUB_TOKEN`.
