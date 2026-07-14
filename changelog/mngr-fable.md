Bump the pinned Claude Code version from 2.1.160 to 2.1.207 (Dockerfile, setup_system.sh, and the `[agent_types.claude].version` pin in .mngr/settings.toml), enabling the Claude Fable 5 model in workspaces. Lands together with the matching mngr PR that bumps the release Dockerfile pin and adds `claude-fable-5` to the LiteLLM proxy.

Switch AskUserQuestion blocking from the raw `cli_args` `--disallowed-tools` list to mngr's new typed `auto_disable_questions = true` setting (requires the vendored mngr to include that field; merge after the vendor/mngr re-sync).
