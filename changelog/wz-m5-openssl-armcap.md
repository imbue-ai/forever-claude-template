- Set `OPENSSL_armcap=0` in `host_env__extend` and at the top of
  `scripts/build_workspace.sh`, working around an Apple M5 + lima-VZ guest
  CPU mismatch where the kernel advertises SVE2 in `/proc/cpuinfo` but the
  VZ guest traps the `cntb` SVE instruction OpenSSL emits during CPU-cap
  init. `cryptography>=47` SIGILLed at import on every M5 launch-to-msg
  CI run; armcap=0 forces NEON-only paths and runs clean on both real
  M-series silicon and lima-VZ.
