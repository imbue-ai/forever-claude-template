- The deferred-install service (`scripts/deferred_install.sh`, documented in
  this README) now also installs the `gitleaks` secret scanner used by the
  publish-inspiration skill: pinned release v8.30.1, downloaded for the
  container's architecture (x86_64 / aarch64) and verified against sha256
  checksums hard-coded in the script, installed to `/usr/local/bin/gitleaks`
  with its own `done.gitleaks` marker. Install failures are isolated (they
  never skip the playwright install) and the marker is only written on
  success so the next boot retries. Unit tests for the new install functions
  live in `libs/bootstrap/src/bootstrap/deferred_install_test.py`.
