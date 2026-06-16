- Hardened `scripts/deferred_install.sh`'s `_recover_interrupted_dpkg` to repair
  packages left in dpkg's reinst-required ("R") state by an interrupted *unpack*
  (e.g. a pool bake's `mngr stop` killing apt mid-install), not just the
  unpacked-but-unconfigured case. After `dpkg --configure -a` it now reinstalls
  any reinst-required package and runs `apt-get --fix-broken install`, so the
  post-lease Playwright/Chromium `--with-deps` install can actually complete
  instead of failing forever on a corrupted `libdbus-1-3` and its dependents.
