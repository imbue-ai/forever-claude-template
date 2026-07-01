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
  logs) moved into `update-service/references/service-processes.md`. Cross-links
  in `build-web-service` (SKILL + `cleanup.md`) and `libs/bootstrap/README.md`
  now point at `update-service`.
