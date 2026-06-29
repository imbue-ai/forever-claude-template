Claude auth recovery now enumerates agents with `mngr list --on-error continue`
and treats the provider-inaccessible exit code as a benign partial success. An
enabled-but-unauthenticated provider (e.g. modal configured but not logged in) no
longer breaks the in-mind Claude login flow: the `type: claude` agents from the
authenticated providers are still listed and restarted. Other `mngr list`
failures continue to raise.
