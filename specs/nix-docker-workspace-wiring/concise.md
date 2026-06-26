# Nix Docker Workspace Wiring Plan

## Goal

Start an FCT workspace through the existing mngr Docker provider while building the host image from `nix/Dockerfile` instead of `Dockerfile`.

The Docker provider already supports this shape: create templates pass `build_arg` values through to `docker build`, and FCT already uses that mechanism for the current Debian Dockerfile.

## Recommended Wiring

Add a new create template in `.mngr/settings.toml` named `docker-nixos`.

Do not stack it after the existing `docker` template. The `docker` template already contributes `--file=Dockerfile` and the build context. Stacking another template that appends `--file=nix/Dockerfile` and `.` would leave multiple Dockerfile/context arguments in the final `docker build` argv.

The new template should duplicate the runtime contract of `[create_templates.docker]`, changing only the build Dockerfile:

```toml
[create_templates.docker-nixos]
provider = "docker"
target_path = "/mngr/code/"
setting__extend = ["providers.docker.is_enabled=true"]
build_arg__extend = ["--file=nix/Dockerfile", "."]
start_arg__extend = ["--security-opt=no-new-privileges", "--workdir=/", "--restart=unless-stopped"]
idle_mode = "disabled"
pass_host_env__extend = ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "GH_TOKEN"]
post_host_create_command__extend = ["/usr/local/bin/fct-seed"]
```

Consider increasing the Docker provider build timeout because first-time Nix image builds can pull a large base and Nix closure:

```toml
[providers.docker]
build_timeout_seconds = 5400
```

## Manual Create Command

Use the new template directly for an experiment:

```bash
mngr create system-services@my-nix-workspace.docker \
  --new-host \
  --template main \
  --template docker-nixos \
  --no-connect
```

This should exercise the same Docker provider path as the current Docker template:

- `docker build --file nix/Dockerfile .`
- `docker run` with the same hardening/start args
- SSH provider setup against the Nix image compatibility paths
- `/usr/local/bin/fct-seed` after host creation
- normal agent provisioning into `/mngr/code/`

## Minds UI Wiring

The Minds UI Docker launch path currently emits `--template main --template docker` in:

`vendor/mngr/apps/minds/imbue/minds/desktop_client/agent_creator.py`

To make Minds create Nix Docker workspaces, choose one of these options:

1. For an experiment, change only the Docker branch to emit `docker-nixos` instead of `docker`.
2. To make all Docker workspaces use Nix, repoint `[create_templates.docker]` itself to `nix/Dockerfile`.
3. To support both modes in the UI, add a distinct launch mode or advanced option that selects `docker` vs `docker-nixos`.

The lowest-risk first step is option 1.

## Tests

Add or update tests for the wiring, separate from the image contract test:

- `test_mngr_template_stacking.py`
  - Assert `main + docker-nixos` preserves the main template's tmux provisioning.
  - Assert `docker-nixos` uses `--file=nix/Dockerfile`.
  - Assert it includes the same hardening `start_arg` values as `docker`.
  - Assert it includes `/usr/local/bin/fct-seed`.

- `vendor/mngr/apps/minds/imbue/minds/desktop_client/agent_creator_test.py`
  - If the Minds UI path is changed, assert Docker launch mode emits `--template docker-nixos`.

- `test_docker_image_contract.py`
  - Already supports the Nix Dockerfile via:

```bash
FCT_DOCKER_IMAGE_CONTRACT=1 \
FCT_DOCKERFILE=nix/Dockerfile \
FCT_DOCKER_IMAGE_TAG=fct-contract:nixos \
FCT_DOCKER_BUILD_TIMEOUT_SECONDS=5400 \
uv run pytest -q -s test_docker_image_contract.py
```

## Remaining Risks

- `scripts/deferred_install.sh` is still Debian-specific and is not covered by the Docker image contract smoke test.
- If any required provider package/path is missing from `nix/Dockerfile`, mngr's runtime repair path will try `apt-get`, which is not valid for the Nix image. The current smoke test checks the important paths and binaries, but a full `mngr create` is still the final integration signal.
- The first uncached Nix Docker build may exceed the default Docker provider timeout unless `build_timeout_seconds` is raised.
