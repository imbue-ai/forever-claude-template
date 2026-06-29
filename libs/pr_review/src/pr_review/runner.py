"""A cleaner interface for reviewing your GitHub pull requests.

A code-aware PR review tool: it lists the viewer's open PRs with their status,
and for each PR fetches the full repo source at the PR commit so the diff renders
in full-file context inside a real editor and the user can open any file in the
repo for context -- without cloning anything by hand.

Services run from /mngr/code (the repo root). Runtime state (the cached repo
source trees) lives under ``runtime/pr-review/`` via cwd-relative paths.

This is a synchronous Flask app served by the threaded Werkzeug server. The
system_interface proxy at ``/service/pr-review/`` rewrites absolute paths in
served HTML and installs a scoped service worker that prepends the prefix to the
page's own fetches, so the app serves at ``/`` and its frontend uses
*relative* fetch URLs (no leading slash) to stay behind the proxy.
"""

from pathlib import Path

from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from werkzeug.serving import run_simple

from pr_review import github
from pr_review import pyintel

app = Flask("pr_review", static_folder=None)

_ASSETS = Path(__file__).parent / "assets"


@app.route("/")
def index() -> Response:
    return Response((_ASSETS / "index.html").read_text(), mimetype="text/html")


@app.route("/app.js")
def app_js() -> Response:
    return Response((_ASSETS / "app.js").read_text(), mimetype="application/javascript")


@app.route("/app.css")
def app_css() -> Response:
    return Response((_ASSETS / "app.css").read_text(), mimetype="text/css")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def _err(message: str, status: int = 502) -> Response:
    response = jsonify({"error": message})
    response.status_code = status
    return response


@app.route("/api/prs")
def api_prs() -> Response:
    try:
        viewer = github.get_viewer()
        return jsonify(github.list_prs(viewer))
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/pr/<owner>/<repo>/<int:number>/status")
def api_pr_status(owner: str, repo: str, number: int) -> Response:
    """Status signals only (CI, review, conflicts, diffstat) -- no source fetch.

    Used to lazily enrich list rows without downloading every repo.
    """
    try:
        return jsonify(github.enrich_status(f"{owner}/{repo}", number))
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/pr/<owner>/<repo>/<int:number>")
def api_pr(owner: str, repo: str, number: int) -> Response:
    full = f"{owner}/{repo}"
    try:
        status = github.enrich_status(full, number)
        files = github.list_changed_files(full, number)
        # Warm the source-tree cache for the head commit so file reads are fast.
        github.ensure_repo_tree(status["head_repo"], status["head_sha"])
        return jsonify({"pr": status, "files": files})
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/pr/<owner>/<repo>/<int:number>/conversation")
def api_conversation(owner: str, repo: str, number: int) -> Response:
    try:
        return jsonify(github.get_conversation(f"{owner}/{repo}", number))
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/pr/<owner>/<repo>/<int:number>/comment", methods=["POST"])
def api_add_comment(owner: str, repo: str, number: int) -> Response:
    body = (request.get_json(silent=True) or {}).get("body", "").strip()
    if not body:
        return _err("comment body is required", status=400)
    try:
        return jsonify(github.add_issue_comment(f"{owner}/{repo}", number, body))
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/pr/<owner>/<repo>/<int:number>/edit", methods=["POST"])
def api_edit_pr(owner: str, repo: str, number: int) -> Response:
    payload = request.get_json(silent=True) or {}
    fields = {k: payload[k] for k in ("title", "body") if k in payload}
    if not fields:
        return _err("expected title and/or body", status=400)
    try:
        return jsonify(github.update_pr(f"{owner}/{repo}", number, fields))
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/pr/<owner>/<repo>/<int:number>/review", methods=["POST"])
def api_create_review(owner: str, repo: str, number: int) -> Response:
    payload = request.get_json(silent=True) or {}
    commit_id = payload.get("commit_id", "")
    body = (payload.get("body") or "").strip()
    event = payload.get("event", "COMMENT")
    comments = payload.get("comments", [])
    if not commit_id:
        return _err("commit_id is required", status=400)
    if not comments and not body:
        return _err("a review needs a body or at least one comment", status=400)
    try:
        return jsonify(github.create_review(f"{owner}/{repo}", number, commit_id, body, event, comments))
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/pr/<owner>/<repo>/<int:number>/file")
def api_pr_file(owner: str, repo: str, number: int) -> Response:
    """Base + head content of one changed file, for the diff editor."""
    full = f"{owner}/{repo}"
    path = request.args.get("path", "")
    head_repo = request.args.get("head_repo", full)
    head_sha = request.args.get("head_sha", "")
    base_sha = request.args.get("base_sha", "")
    status = request.args.get("status", "modified")
    previous = request.args.get("previous_path") or path
    if not path or not head_sha:
        return _err("path and head_sha are required", status=400)
    try:
        tree = github.ensure_repo_tree(head_repo, head_sha)
        head_content = "" if status == "removed" else (github.read_tree_file(tree, path) or "")
        base_content = ""
        if status not in ("added",) and base_sha:
            base_content = github.get_file_at_ref(full, previous, base_sha) or ""
        return jsonify({
            "path": path,
            "base": base_content,
            "head": head_content,
            "binary": head_content == "" and status not in ("removed", "added"),
        })
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/repo/<owner>/<repo>/<sha>/tree")
def api_repo_tree(owner: str, repo: str, sha: str) -> Response:
    """Every file path in the repo at ``sha`` -- powers open-any-file."""
    full = f"{owner}/{repo}"
    try:
        tree = github.ensure_repo_tree(full, sha)
        return jsonify({"files": github.list_tree_files(tree)})
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/repo/<owner>/<repo>/<sha>/usages")
def api_repo_usages(owner: str, repo: str, sha: str) -> Response:
    """Find-usages / go-to-definition for a symbol across the cached repo."""
    full = f"{owner}/{repo}"
    name = request.args.get("name", "")
    if not name:
        return _err("name is required", status=400)
    try:
        tree = github.ensure_repo_tree(full, sha)
        return jsonify(github.find_usages(tree, name))
    except github.GitHubError as exc:
        return _err(str(exc), status=400)


@app.route("/api/repo/<owner>/<repo>/<sha>/pyhover")
def api_pyhover(owner: str, repo: str, sha: str) -> Response:
    """Type-aware hover for a Python symbol (Jedi)."""
    full = f"{owner}/{repo}"
    path = request.args.get("path", "")
    try:
        line = int(request.args.get("line", "0"))
        col = int(request.args.get("col", "0"))
    except ValueError:
        return _err("line and col must be integers", status=400)
    if not path:
        return _err("path is required", status=400)
    try:
        tree = github.ensure_repo_tree(full, sha)
        result = pyintel.hover(tree, path, line, col)
        return jsonify(result or {"contents": ""})
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/repo/<owner>/<repo>/<sha>/pydef")
def api_pydef(owner: str, repo: str, sha: str) -> Response:
    """Go-to-definition for a Python symbol (Jedi)."""
    full = f"{owner}/{repo}"
    path = request.args.get("path", "")
    try:
        line = int(request.args.get("line", "0"))
        col = int(request.args.get("col", "0"))
    except ValueError:
        return _err("line and col must be integers", status=400)
    if not path:
        return _err("path is required", status=400)
    try:
        tree = github.ensure_repo_tree(full, sha)
        result = pyintel.definition(tree, path, line, col)
        return jsonify(result or {"found": False})
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/repo/<owner>/<repo>/<sha>/file")
def api_repo_file(owner: str, repo: str, sha: str) -> Response:
    """Arbitrary file content from the cached tree (open-any-file)."""
    full = f"{owner}/{repo}"
    path = request.args.get("path", "")
    if not path:
        return _err("path is required", status=400)
    try:
        tree = github.ensure_repo_tree(full, sha)
        content = github.read_tree_file(tree, path)
        if content is None:
            return _err("file not found or binary", status=404)
        return jsonify({"path": path, "content": content})
    except github.GitHubError as exc:
        return _err(str(exc))


def main() -> None:
    run_simple("127.0.0.1", 8081, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
