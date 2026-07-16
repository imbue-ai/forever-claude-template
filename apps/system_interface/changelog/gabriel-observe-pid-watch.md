The system interface now drives each agent's lifecycle state from the full `mngr observe` stream (`mngr observe --stream-events`) instead of the state-less `--discovery-only` stream.

- Agents now show their real, live lifecycle state (RUNNING / WAITING / STOPPED / DONE / ...) rather than being pinned to RUNNING. In particular, an agent whose process dies on its own (crash, OOM, or normal exit) is now reflected as stopped within seconds, and its "Thinking..." activity indicator correctly drops to idle via the existing activity gate.

- Agent membership (create / destroy) still propagates promptly: the observer emits per-agent state on creation and a new removal event on destroy, which the interface folds into its live agent list.

- Only the source of the lifecycle state changed; the liveness dot, dot-shape, and activity-indicator UI logic are unchanged. Depends on the corresponding `mngr` change (the `--stream-events` observe mode) being vendored in.
