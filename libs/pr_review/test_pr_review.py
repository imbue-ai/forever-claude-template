"""Route-level tests for the PR-review Flask app.

Driven entirely through ``app.test_client()`` in-process -- no real server, no
GitHub network, no writes. Routes that need a repo source tree are served from a
pre-seeded on-disk cache (``seed_repo_cache``) so ``ensure_repo_tree`` returns
without ever calling the network. Routes that hit the live GitHub API on their
happy path (PR list/status/conversation, comment/edit/review writes) are covered
at the function level in ``github_test.py``; here we assert their request
validation and the error-to-HTTP mapping that the route layer owns.
"""

from pathlib import Path

import pytest
from flask.testing import FlaskClient
from pr_review.testing import seed_repo_cache

_SHA = "abc123"
_REPO = "octocat/hello"


# --- static + health ---


def test_health_ok(client: FlaskClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_index_served(client: FlaskClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert b"<" in resp.data


def test_app_js_served(client: FlaskClient) -> None:
    resp = client.get("/app.js")
    assert resp.status_code == 200
    assert resp.mimetype == "application/javascript"


def test_app_css_served(client: FlaskClient) -> None:
    resp = client.get("/app.css")
    assert resp.status_code == 200
    assert resp.mimetype == "text/css"


# --- request validation (no network reached) ---


def test_add_comment_requires_body(client: FlaskClient) -> None:
    resp = client.post(f"/api/pr/{_REPO}/1/comment", json={"body": "   "})
    assert resp.status_code == 400
    assert "required" in resp.get_json()["error"]


def test_edit_pr_requires_fields(client: FlaskClient) -> None:
    resp = client.post(f"/api/pr/{_REPO}/1/edit", json={"state": "closed"})
    assert resp.status_code == 400


def test_set_state_rejects_invalid_state(client: FlaskClient) -> None:
    resp = client.post(f"/api/pr/{_REPO}/1/state", json={"state": "merged"})
    assert resp.status_code == 400
    assert "state" in resp.get_json()["error"]


def test_merge_rejects_invalid_method(client: FlaskClient) -> None:
    resp = client.post(f"/api/pr/{_REPO}/1/merge", json={"method": "fast-forward"})
    assert resp.status_code == 400
    assert "method" in resp.get_json()["error"]


def test_create_review_requires_commit_id(client: FlaskClient) -> None:
    resp = client.post(f"/api/pr/{_REPO}/1/review", json={"body": "ok"})
    assert resp.status_code == 400
    assert "commit_id" in resp.get_json()["error"]


def test_create_review_requires_body_or_comment(client: FlaskClient) -> None:
    resp = client.post(f"/api/pr/{_REPO}/1/review", json={"commit_id": "deadbeef"})
    assert resp.status_code == 400


def test_pr_file_requires_path_and_head_sha(client: FlaskClient) -> None:
    resp = client.get(f"/api/pr/{_REPO}/1/file")
    assert resp.status_code == 400


def test_usages_requires_name(client: FlaskClient) -> None:
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/usages")
    assert resp.status_code == 400


def test_pyhover_requires_path(client: FlaskClient) -> None:
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/pyhover?line=1&col=1")
    assert resp.status_code == 400


def test_pyhover_rejects_non_integer_position(client: FlaskClient) -> None:
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/pyhover?path=mod.py&line=x&col=1")
    assert resp.status_code == 400


def test_pydef_requires_path(client: FlaskClient) -> None:
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/pydef?line=1&col=1")
    assert resp.status_code == 400


def test_jshover_requires_path(client: FlaskClient) -> None:
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/jshover?line=1&col=1")
    assert resp.status_code == 400


def test_jshover_rejects_non_integer_position(client: FlaskClient) -> None:
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/jshover?path=mod.ts&line=x&col=1")
    assert resp.status_code == 400


def test_jsdef_requires_path(client: FlaskClient) -> None:
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/jsdef?line=1&col=1")
    assert resp.status_code == 400


# --- cache-backed repo routes (served from disk, no network) ---


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    seed_repo_cache(
        _REPO,
        _SHA,
        {
            "defs.py": "def widget() -> int:\n    return 1\n",
            "main.py": "from defs import widget\n\nwidget()\n",
        },
    )


def test_repo_tree_lists_files(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/tree")
    assert resp.status_code == 200
    assert resp.get_json()["files"] == ["defs.py", "main.py"]


def test_repo_file_returns_content(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/file?path=defs.py")
    assert resp.status_code == 200
    assert "def widget" in resp.get_json()["content"]


def test_repo_file_missing_returns_404(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/file?path=ghost.py")
    assert resp.status_code == 404


def test_repo_file_path_escape_maps_to_error(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/file?path=../escape.py")
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_repo_usages_finds_symbol(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/usages?name=widget")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["symbol"] == "widget"
    assert body["total"] == 3


def test_repo_usages_invalid_symbol_maps_to_400(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/usages?name=not+valid")
    assert resp.status_code == 400


def test_pyhover_returns_contents(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/pyhover?path=main.py&line=3&col=1")
    assert resp.status_code == 200
    assert "widget" in resp.get_json()["contents"]


def test_pydef_resolves_in_repo(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/pydef?path=main.py&line=3&col=1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["in_repo"] is True
    assert body["path"] == "defs.py"


def _seed_ts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    seed_repo_cache(
        _REPO,
        _SHA,
        {
            "util.ts": "export function widget(): number {\n  return 1;\n}\n",
            "main.ts": 'import { widget } from "./util";\n\nwidget();\n',
        },
    )


def test_jshover_returns_contents(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_ts(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/jshover?path=main.ts&line=3&col=1")
    assert resp.status_code == 200
    assert "widget" in resp.get_json()["contents"]


def test_jsdef_resolves_in_repo(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_ts(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/jsdef?path=main.ts&line=3&col=1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["in_repo"] is True
    assert body["path"] == "util.ts"
