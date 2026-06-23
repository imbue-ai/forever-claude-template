# Removing a web service

1. `python3 scripts/forward_port.py --name <name> --remove` (drops the
   entry from `runtime/applications.toml`).
2. Stop the program and remove its block from `supervisord.conf`, then
   reconcile:

   ```bash
   supervisorctl stop <name>
   # delete the [program:<name>] block from supervisord.conf
   supervisorctl reread && supervisorctl update
   ```

   (See `edit-services` for the mechanics.)
3. If you scaffolded a lib, also: `rm -rf libs/<package>/` and revert
   the matching diff in the root `pyproject.toml` (drop from
   `[project].dependencies`, `[tool.uv.workspace].members`, and
   `[tool.uv.sources]`).
