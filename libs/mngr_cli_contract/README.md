# mngr-cli-contract

A small test-only helper that asserts a `mngr <subcommand> ...` argv is
structurally accepted by the **live** `imbue.mngr.main.cli` click command tree
(subcommand exists, option tokens recognized), using click's low-level parser
so value validators do not run.

It exists because repo code shells out to the `mngr` CLI, and tests that pin
those invocations against hand-written expected argvs cannot catch a vendor/mngr
subcommand/flag rename -- both the production string and the mirrored test
string drift together. `assert_mngr_argv_valid` confronts the emitted argv with
the real CLI surface instead, so that class of breakage fails at merge time.

This is its own workspace package (rather than a module in one project) so the
root pytest pass and the isolated `apps/system_interface` pass -- which share a
single workspace venv -- import one copy.

```python
from mngr_cli_contract.contract import assert_mngr_argv_valid

assert_mngr_argv_valid(["mngr", "create", "demo", "-t", "worker"])
```
