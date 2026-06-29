Removed the last asyncio usage from the agent-facing skills shipped with the template, as part of the FastAPI-to-Flask migration of the web stack:

- The `build-web-service` skill now scaffolds Flask + flask-sock services instead of FastAPI/uvicorn. The scaffolder script was renamed `scaffold_fastapi_lib.py` -> `scaffold_flask_lib.py`; the generated `runner.py` is a synchronous Flask app served by the threaded Werkzeug server. The system_interface proxy handles `/service/<name>/` prefixing, so the generated app serves at `/` (no `ROOT_PATH`). The SKILL docs and references were updated to match.

- The `use-ai-integration` skill's copyable `claude_p.py` helper is now synchronous (dropped `anyio`/`await`), and its examples (including the litellm path) use synchronous calls; batch concurrency is now via a thread pool rather than an async task group.
