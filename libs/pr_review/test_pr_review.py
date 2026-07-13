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
from pr_review import ask, github, prepare
from pr_review.runner import app
from pr_review.testing import requires_ripgrep, seed_prepared_state, seed_repo_cache

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


def test_repo_tree_lists_files(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/tree")
    assert resp.status_code == 200
    assert resp.get_json()["files"] == ["defs.py", "main.py"]


def test_repo_file_returns_content(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/file?path=defs.py")
    assert resp.status_code == 200
    assert "def widget" in resp.get_json()["content"]


def test_repo_file_missing_returns_404(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/file?path=ghost.py")
    assert resp.status_code == 404


def test_repo_file_path_escape_maps_to_error(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/file?path=../escape.py")
    assert resp.status_code == 502
    assert "error" in resp.get_json()


@requires_ripgrep
def test_repo_usages_finds_symbol(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/usages?name=widget")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["symbol"] == "widget"
    assert body["total"] == 3


def test_repo_usages_invalid_symbol_maps_to_400(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/usages?name=not+valid")
    assert resp.status_code == 400


def test_pyhover_returns_contents(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/pyhover?path=main.py&line=3&col=1")
    assert resp.status_code == 200
    assert "widget" in resp.get_json()["contents"]


def test_pydef_resolves_in_repo(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_jshover_returns_contents(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_ts(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/jshover?path=main.ts&line=3&col=1")
    assert resp.status_code == 200
    assert "widget" in resp.get_json()["contents"]


def test_jsdef_resolves_in_repo(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_ts(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/jsdef?path=main.ts&line=3&col=1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["in_repo"] is True
    assert body["path"] == "util.ts"


def test_prepare_status_absent_before_prepare(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_ts(tmp_path, monkeypatch)
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/prepare/status")
    assert resp.status_code == 200
    assert resp.get_json()["state"] == "absent"


def test_prepare_launches_and_reports_installing(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_ts(tmp_path, monkeypatch)
    launched: list = []
    app.config["PREPARE_LAUNCHER"] = launched.append
    try:
        resp = client.post(f"/api/repo/{_REPO}/{_SHA}/prepare", json={})
        assert resp.status_code == 200
        assert resp.get_json()["state"] == "installing"
        assert len(launched) == 1
        status = client.get(f"/api/repo/{_REPO}/{_SHA}/prepare/status")
        assert status.get_json()["state"] == "installing"
    finally:
        app.config.pop("PREPARE_LAUNCHER", None)


def test_jshover_falls_back_to_treesitter_when_rich_unavailable(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_ts(tmp_path, monkeypatch)
    # Mark the tree "ready" but point at a typescript_dir that does not exist, so
    # the rich engine cannot start and the route must fall back to tree-sitter.
    tree = github.ensure_repo_tree(_REPO, _SHA)
    prepare._write_status(tree, {"state": "ready", "typescript_dir": "does-not-exist"})
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/jshover?path=main.ts&line=3&col=1")
    assert resp.status_code == 200
    assert "widget" in resp.get_json()["contents"]


def test_prepare_clear_resets_state(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_ts(tmp_path, monkeypatch)
    app.config["PREPARE_LAUNCHER"] = lambda _tree: None
    try:
        client.post(f"/api/repo/{_REPO}/{_SHA}/prepare", json={})
        resp = client.post(f"/api/repo/{_REPO}/{_SHA}/prepare/clear", json={})
        assert resp.status_code == 200
        assert resp.get_json()["state"] == "absent"
        status = client.get(f"/api/repo/{_REPO}/{_SHA}/prepare/status")
        assert status.get_json()["state"] == "absent"
    finally:
        app.config.pop("PREPARE_LAUNCHER", None)


def test_prepare_status_auto_enables_from_shared_store(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A TS repo whose dependency manifest gives it a reusable fingerprint.
    monkeypatch.chdir(tmp_path)
    seed_repo_cache(
        _REPO,
        _SHA,
        {
            "util.ts": "export const x = 1;\n",
            "package.json": '{"name":"app","dependencies":{"left-pad":"1"}}',
            "package-lock.json": '{"lockfileVersion":3}',
        },
    )
    tree = github.ensure_repo_tree(_REPO, _SHA)
    # Publish a completed install for this dependency set to the shared store, then
    # clear the local checkout so its own state is "absent" again (store survives).
    seed_prepared_state(tree, ["."])
    prepare._publish(tree, prepare.dep_fingerprint(tree.root), ["."])
    prepare.clear_prepared(tree)
    assert prepare.prepare_status(tree)["state"] == "absent"

    # Polling status auto-enables from the store with no agent: the pill flips to
    # "rich" on its own and the sidecar is a symlink into the shared store.
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/prepare/status")
    assert resp.status_code == 200
    assert resp.get_json()["state"] == "ready"
    assert (tree.root / prepare.PREP_DIRNAME).is_symlink()


def test_prepare_status_stays_absent_without_store_match(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A repo with dependencies but nothing published for it: auto-enable is a
    # no-op, so rich types stay opt-in behind the explicit Enable action.
    monkeypatch.chdir(tmp_path)
    seed_repo_cache(
        _REPO,
        _SHA,
        {"util.ts": "export const x = 1;\n", "package.json": '{"name":"app"}'},
    )
    resp = client.get(f"/api/repo/{_REPO}/{_SHA}/prepare/status")
    assert resp.status_code == 200
    assert resp.get_json()["state"] == "absent"
    tree = github.ensure_repo_tree(_REPO, _SHA)
    assert not (tree.root / prepare.PREP_DIRNAME).exists()


# --- ask-an-agent (per-line questions) ---


def test_ask_requires_question(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.post(
        f"/api/pr/{_REPO}/1/ask", json={"path": "main.py", "line": 3, "head_sha": _SHA}
    )
    assert resp.status_code == 400
    assert "required" in resp.get_json()["error"]


def test_ask_requires_head_sha(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.post(
        f"/api/pr/{_REPO}/1/ask",
        json={"path": "main.py", "line": 3, "question": "why?"},
    )
    assert resp.status_code == 400


def test_ask_rejects_bad_side(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.post(
        f"/api/pr/{_REPO}/1/ask",
        json={
            "path": "main.py",
            "line": 3,
            "question": "why?",
            "head_sha": _SHA,
            "side": "MIDDLE",
        },
    )
    assert resp.status_code == 400


def test_ask_launches_and_persists(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    launched: list = []
    app.config["ASK_LAUNCHER"] = launched.append
    try:
        resp = client.post(
            f"/api/pr/{_REPO}/7/ask",
            json={
                "path": "main.py",
                "line": 3,
                "question": "what runs here?",
                "head_sha": _SHA,
            },
        )
        assert resp.status_code == 200
        rec = resp.get_json()
        assert rec["state"] == "running"
        assert len(launched) == 1
        # Listable and fetchable by id.
        listed = client.get(f"/api/pr/{_REPO}/7/questions").get_json()["questions"]
        assert [r["id"] for r in listed] == [rec["id"]]
        status = client.get(f"/api/pr/{_REPO}/7/questions/{rec['id']}")
        assert status.status_code == 200
        assert status.get_json()["question"] == "what runs here?"
    finally:
        app.config.pop("ASK_LAUNCHER", None)


def test_question_status_unknown_is_404(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    resp = client.get(f"/api/pr/{_REPO}/7/questions/nope123")
    assert resp.status_code == 404


def test_delete_question(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, monkeypatch)
    tree = github.ensure_repo_tree(_REPO, _SHA)
    rec = ask.create_question(
        tree,
        _REPO,
        8,
        path="main.py",
        line=3,
        side="RIGHT",
        question="q",
        launcher=lambda _t: None,
    )
    resp = client.post(f"/api/pr/{_REPO}/8/questions/{rec['id']}/delete", json={})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert client.get(f"/api/pr/{_REPO}/8/questions").get_json()["questions"] == []
