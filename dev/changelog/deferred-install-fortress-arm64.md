`scripts/deferred_install.sh` now fetches the Fortress stealth Chromium fork
per-arch on first container boot instead of vanilla Playwright Chromium: x64
from Fortress's official `v151.0.7908.0` release, arm64 from a fork build
(minhtrinh-imbue/fortress, for the aarch64-support PR tiliondev/fortress#29
until it merges upstream). Both tarballs are SHA256-verified before install.
