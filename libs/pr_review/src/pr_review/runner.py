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

import os
from pathlib import Path

from flask import Flask, Response, jsonify, request
from werkzeug.serving import run_simple

from pr_review import ask, github, jsintel, prepare, pyintel, tsintel

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
        # Warm the source-tree cache for the head commit so file reads are fast,
        # and silently turn on rich types if a matching prep can be reused with no
        # install agent (a no-op otherwise).
        tree = github.ensure_repo_tree(status["head_repo"], status["head_sha"])
        prepare.auto_enable(tree)
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


@app.route("/api/pr/<owner>/<repo>/<int:number>/state", methods=["POST"])
def api_set_state(owner: str, repo: str, number: int) -> Response:
    """Close or reopen a PR."""
    state = (request.get_json(silent=True) or {}).get("state", "")
    if state not in ("open", "closed"):
        return _err("state must be 'open' or 'closed'", status=400)
    try:
        return jsonify(github.set_pr_state(f"{owner}/{repo}", number, state))
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/pr/<owner>/<repo>/<int:number>/merge", methods=["POST"])
def api_merge(owner: str, repo: str, number: int) -> Response:
    """Merge a PR with the given method (merge / squash / rebase)."""
    method = (request.get_json(silent=True) or {}).get("method", "merge")
    if method not in ("merge", "squash", "rebase"):
        return _err("method must be 'merge', 'squash', or 'rebase'", status=400)
    try:
        return jsonify(github.merge_pr(f"{owner}/{repo}", number, method))
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


@app.route("/api/pr/<owner>/<repo>/<int:number>/questions")
def api_list_questions(owner: str, repo: str, number: int) -> Response:
    """Every saved "ask an agent" question for a PR (to restore them in the diff)."""
    return jsonify({"questions": ask.list_questions(f"{owner}/{repo}", number)})


@app.route("/api/pr/<owner>/<repo>/<int:number>/ask", methods=["POST"])
def api_ask(owner: str, repo: str, number: int) -> Response:
    """Launch a read-only local agent to investigate a question about a diff line."""
    full = f"{owner}/{repo}"
    payload = request.get_json(silent=True) or {}
    path = (payload.get("path") or "").strip()
    question = (payload.get("question") or "").strip()
    head_repo = payload.get("head_repo") or full
    head_sha = payload.get("head_sha") or ""
    side = payload.get("side") or "RIGHT"
    model = payload.get("model")
    try:
        line = int(payload.get("line", 0))
    except (TypeError, ValueError):
        return _err("line must be an integer", status=400)
    if not path or not question:
        return _err("path and question are required", status=400)
    if not head_sha:
        return _err("head_sha is required", status=400)
    if side not in ("LEFT", "RIGHT"):
        return _err("side must be 'LEFT' or 'RIGHT'", status=400)
    try:
        tree = github.ensure_repo_tree(head_repo, head_sha)
        # ASK_LAUNCHER lets tests inject a fake launcher; None -> real agent.
        launcher = app.config.get("ASK_LAUNCHER")
        return jsonify(ask.create_question(
            tree, full, number, path=path, line=line, side=side,
            question=question, model=model, launcher=launcher,
        ))
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/pr/<owner>/<repo>/<int:number>/questions/<qid>")
def api_question_status(owner: str, repo: str, number: int, qid: str) -> Response:
    """One question's current state + streamed investigation log (for polling)."""
    result = ask.question_status(f"{owner}/{repo}", number, qid)
    if result is None:
        return _err("question not found", status=404)
    return jsonify(result)


@app.route("/api/pr/<owner>/<repo>/<int:number>/questions/<qid>/delete", methods=["POST"])
def api_delete_question(owner: str, repo: str, number: int, qid: str) -> Response:
    """Remove a saved question (its record and log)."""
    return jsonify(ask.delete_question(f"{owner}/{repo}", number, qid))


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


@app.route("/api/repo/<owner>/<repo>/<sha>/jshover")
def api_jshover(owner: str, repo: str, sha: str) -> Response:
    """Declaration-aware hover for a JavaScript/TypeScript symbol (tree-sitter)."""
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
        # Rich types (tsserver) for prepared repos, tree-sitter otherwise or as a
        # fallback when the language service has nothing / errors. auto_enable turns
        # rich types on for free if a reusable prep matches (no agent needed).
        ready = prepare.auto_enable(tree).get("state") == "ready"
        result = tsintel.hover(tree, path, line, col) if ready else None
        result = result or jsintel.hover(tree, path, line, col)
        return jsonify(result or {"contents": ""})
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/repo/<owner>/<repo>/<sha>/jsdef")
def api_jsdef(owner: str, repo: str, sha: str) -> Response:
    """Go-to-definition for a JavaScript/TypeScript symbol (tree-sitter)."""
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
        ready = prepare.auto_enable(tree).get("state") == "ready"
        result = tsintel.definition(tree, path, line, col) if ready else None
        result = result or jsintel.definition(tree, path, line, col)
        return jsonify(result or {"found": False})
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/repo/<owner>/<repo>/<sha>/prepare", methods=["POST"])
def api_prepare(owner: str, repo: str, sha: str) -> Response:
    """Opt-in: launch the agent that installs deps + sets up rich (tsserver) types."""
    full = f"{owner}/{repo}"
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force"))
    model = payload.get("model")
    try:
        tree = github.ensure_repo_tree(full, sha)
        # PREPARE_LAUNCHER lets tests inject a fake launcher; None -> real agent.
        launcher = app.config.get("PREPARE_LAUNCHER")
        return jsonify(prepare.start_prepare(tree, launcher=launcher, force=force, model=model))
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/repo/<owner>/<repo>/<sha>/prepare/status")
def api_prepare_status(owner: str, repo: str, sha: str) -> Response:
    """Current rich-types preparation state for a repo tree, with a log tail."""
    full = f"{owner}/{repo}"
    try:
        tree = github.ensure_repo_tree(full, sha)
        # Reuse a matching prep with no agent if one exists, so the pill shows
        # "rich" without the user clicking Enable.
        status = prepare.auto_enable(tree)
        status["log_tail"] = prepare.log_tail(tree)
        return jsonify(status)
    except github.GitHubError as exc:
        return _err(str(exc))


@app.route("/api/repo/<owner>/<repo>/<sha>/prepare/clear", methods=["POST"])
def api_prepare_clear(owner: str, repo: str, sha: str) -> Response:
    """Drop prepared state + installed node_modules for a repo tree (reclaim disk)."""
    full = f"{owner}/{repo}"
    try:
        tree = github.ensure_repo_tree(full, sha)
        return jsonify(prepare.clear_prepared(tree))
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
    port = int(os.environ.get("PR_REVIEW_PORT", "8082"))
    run_simple("127.0.0.1", port, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
