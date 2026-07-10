- Reorganized the service-editing skills so that changing an existing service
  has a clear front door. The old `edit-services` skill (which only covered the
  supervisord `[program:*]` plumbing and was usually skipped when an agent just
  edited service code) is replaced by **`update-service`**: a front door that
  triggers on editing/fixing/restyling/extending an existing service -- web
  service or background daemon -- and owns the live change loop (apply the change
  so it takes effect, refresh the user's tab for web services, verify) plus the
  turn-end handoff to the `update-artifact` / `heal-artifact` hardening core.

- `update-service` routes by scope: a contained change goes straight through the
  live loop, while a larger-scope change (redesign, new view, look-and-feel
  shift) is sent back through the same incremental interactive-delivery flow
  (`build-web-service`'s mock-confirm loop) used to build the service in the
  first place, so heavy work never happens against an unconfirmed shape.

- The supervisord program mechanics (program schema, add/remove/modify/inspect,
  logs) live in the shared reference
  `.agents/shared/references/service-processes.md` -- substrate mechanics common
  to every service flow, not specific to updating one. `update-service`,
  `build-web-service` (SKILL + `cleanup.md`), and `libs/bootstrap/README.md` all
  cross-link to it.

- Extracted the "spin up an isolated throwaway instance of a service on a spare
  port" mechanism into a shared, unopinionated script
  `.agents/shared/scripts/serve_isolated_instance.py` (`up` / `down`). It takes
  the launch command, cwd, and env overrides as parameters, picks a free port,
  injects it, waits for health, and either hands back the loopback URL (for the
  agent's own data-safe testing) or -- given `--service-name` +
  `--preview-service-name`/`--preview-title` -- registers it and wraps it in the
  labeled "preview" frame (the moved `preview_wrapper_server.py`) as a tab. Both
  `update-service` (data-isolated verification, optional user-facing preview) and
  `update-system-interface` now use it.

- `update-system-interface`'s `preview` / `unpreview` are now thin adapters that
  delegate the boot/teardown to that shared script, passing the system-interface
  specifics (neuter layout persistence, probe `/api/agents`, register the inner
  app + wrapper). The merge and reveal/auto-rollback machinery -- the part whose
  failure would strand the user with no UI -- stays owned by
  `reveal_system_interface.py`; it was intentionally *not* generalized.

- Scaffolded Flask services now read their listen port from a
  `<PACKAGE_UPPER>_PORT` env override (mirroring the existing
  `<PACKAGE_UPPER>_DATA_DIR`), so a throwaway instance can bind a spare port
  beside the live one. `update-service` tells agents to retrofit both overrides
  onto older services when they touch them.
