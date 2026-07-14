- The two secret-scanner binaries the publish-inspiration skill hard-requires
  -- `betterleaks` v1.6.1 (MIT, replacing gitleaks) and `kingfisher` v1.106.0
  (Apache-2.0) -- are installed by a new shared
  `scripts/install_secret_scanners.sh`, the single source of truth for the
  version pins and hard-coded per-arch (x86_64 / aarch64) sha256 checksums.
  The common `scripts/setup_system.sh` invokes it at build/provision time, so
  the scanners exist from the first second of every workspace -- both
  docker-built images (Dockerfile RUN of `setup_system.sh`) and
  Lima-provisioned VMs (which run `setup_system.sh` directly in the VM). If a
  binary is ever missing, the script is runnable by hand to install both (it
  skips any tool already present at its pinned version without network access,
  so a redundant run is an instant no-op); the scan gate names that command in
  its missing-scanner error. The deferred-install service does NOT deliver the
  scanners -- it stays limited to heavy non-boot packages (Chromium/Playwright).

- `install_secret_scanners_test.py` covers the shared installer (arch mapping,
  checksum accept/reject, skip-at-pin, per-tool isolation), with shared
  bash-test helpers in `bootstrap/testing.py`.
