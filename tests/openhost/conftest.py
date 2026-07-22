import tomllib
from pathlib import Path

import pytest
from openhost_test_harness import OpenhostStack

# First boot builds the image (slow when the podman layer cache is cold),
# seeds the workspace onto the volume, and creates the system-services agent
# before system_interface starts answering the health probe.
DEPLOY_TIMEOUT = 1800.0


LATCHKEY_REPO_URL = "https://github.com/imbue-openhost/openhost-latchkey"


@pytest.fixture(scope="session")
def stack():
    def _deploy_latchkey(s: OpenhostStack) -> None:
        s.deploy_app(LATCHKEY_REPO_URL)

    with OpenhostStack(deploy_timeout=DEPLOY_TIMEOUT, pre_deploy=_deploy_latchkey) as s:
        yield s


@pytest.fixture(scope="session")
def app_name() -> str:
    manifest = Path(__file__).parent.parent.parent / "openhost.toml"
    return tomllib.loads(manifest.read_text())["app"]["name"]


@pytest.fixture(scope="session")
def container_name(app_name) -> str:
    return f"openhost-{app_name}"


@pytest.fixture(scope="session")
def app_data_dir(stack, app_name) -> Path:
    return Path(stack.local_stack.config.persistent_data_dir) / "app_data" / app_name
