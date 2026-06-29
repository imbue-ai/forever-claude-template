"""GitHub access for the PR-review service.

All GitHub calls go through ``latchkey curl`` so the user's stored credentials
are injected transparently -- there is never a token in this process. The
service fetches the authenticated viewer's open PRs (authored + review-requested)
with the status signals shown in the list, and lazily fetches+caches each PR's
full source tree (at the PR head commit) on disk so the diff view can render
files in full context and let the user open any file in the repo.
"""

import base64
import json
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

API = "https://api.github.com"
REPO_CACHE = Path("runtime/pr-review/repos")

# The transport seam: a callable that runs ``latchkey curl`` with the given
# argument list and returns stdout bytes. Every network function takes one as an
# injectable parameter defaulting to the real ``_curl`` -- production callers use
# the default, while tests pass a fake so no real GitHub call or write ever runs.
CurlFn = Callable[[list[str]], bytes]


class GitHubError(RuntimeError):
    """A GitHub call through latchkey failed."""


def _curl(args: list[str]) -> bytes:
    result = subprocess.run(
        ["latchkey", "curl", "-s", *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitHubError(f"latchkey curl failed ({result.returncode}): {result.stderr.decode(errors='replace')[:500]}")
    return result.stdout


def gh_json(path: str, curl: CurlFn = _curl) -> dict | list:
    """GET a GitHub REST endpoint and parse JSON. ``path`` is relative to the API root."""
    url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
    raw = curl([url])
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubError(f"non-JSON response from {url}: {raw[:300]!r}") from exc


_STATUS_MARKER = "\n__HTTP_STATUS__"


def gh_request(method: str, path: str, payload: dict | None = None, curl: CurlFn = _curl) -> dict:
    """Make a write request (POST/PATCH/DELETE) and return the parsed JSON body.

    Raises GitHubError with the API message on any non-2xx status.
    """
    url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
    args = ["-sS", "-w", _STATUS_MARKER + "%{http_code}", "-X", method, "-H", "Content-Type: application/json", url]
    if payload is not None:
        args = ["-d", json.dumps(payload), *args]
    out = curl(args).decode(errors="replace")
    idx = out.rfind(_STATUS_MARKER)
    status = int(out[idx + len(_STATUS_MARKER):]) if idx >= 0 else 0
    body_text = out[:idx] if idx >= 0 else out
    data = json.loads(body_text) if body_text.strip() else {}
    if status >= 300:
        message = data.get("message") if isinstance(data, dict) else None
        raise GitHubError(f"GitHub {method} {path} -> {status}: {message or body_text[:200]}")
    return data if isinstance(data, dict) else {"result": data}


def get_viewer(curl: CurlFn = _curl) -> str:
    """The authenticated user's login."""
    me = gh_json("user", curl)
    assert isinstance(me, dict)
    login = me.get("login")
    if not login:
        raise GitHubError(f"could not resolve viewer login: {me}")
    return login


# ---------------------------------------------------------------------------
# PR list + status enrichment
# ---------------------------------------------------------------------------


def _ci_verdict(check_runs: dict, combined: dict) -> dict:
    counts = {"success": 0, "failure": 0, "pending": 0, "neutral": 0}
    for run in check_runs.get("check_runs", []):
        if run.get("status") != "completed":
            counts["pending"] += 1
        elif run.get("conclusion") == "success":
            counts["success"] += 1
        elif run.get("conclusion") in ("failure", "timed_out", "cancelled"):
            counts["failure"] += 1
        else:
            counts["neutral"] += 1
    # Legacy combined commit status only counts when statuses actually exist --
    # the endpoint defaults to "pending" with zero statuses, which would wrongly
    # override a clean check-runs result.
    overall = combined.get("state") if combined.get("total_count", 0) > 0 else None
    if counts["failure"] or overall == "failure":
        verdict = "failing"
    elif counts["pending"] or overall == "pending":
        verdict = "pending"
    elif counts["success"] or overall == "success":
        verdict = "passing"
    else:
        verdict = "none"
    return {"verdict": verdict, "counts": counts}


def _review_decision(reviews: list) -> str:
    by_user: dict[str, str] = {}
    for review in reviews:
        state = review.get("state")
        login = (review.get("user") or {}).get("login")
        if login and state in ("APPROVED", "CHANGES_REQUESTED"):
            by_user[login] = state
    states = set(by_user.values())
    if "CHANGES_REQUESTED" in states:
        return "changes requested"
    if "APPROVED" in states:
        return "approved"
    if reviews:
        return "commented"
    return "none"


def _summarize_search_item(item: dict, viewer: str) -> dict:
    """A lightweight row from a search result (no per-PR extra calls)."""
    repo = item["repository_url"].replace(f"{API}/repos/", "")
    return {
        "repo": repo,
        "number": item["number"],
        "title": item["title"],
        "author": (item.get("user") or {}).get("login"),
        "is_mine": (item.get("user") or {}).get("login") == viewer,
        "state": "draft" if item.get("draft") else "ready",
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "comments": item.get("comments", 0),
        "url": item.get("html_url"),
    }


def list_prs(viewer: str, curl: CurlFn = _curl) -> dict:
    """Both buckets of the viewer's open PRs, lightweight (no per-PR enrichment)."""
    authored = gh_json(f"search/issues?q=is:open+is:pr+author:{viewer}&per_page=100", curl)
    requested = gh_json(f"search/issues?q=is:open+is:pr+review-requested:{viewer}&per_page=100", curl)
    assert isinstance(authored, dict) and isinstance(requested, dict)
    return {
        "viewer": viewer,
        "authored": [_summarize_search_item(it, viewer) for it in authored.get("items", [])],
        "review_requested": [_summarize_search_item(it, viewer) for it in requested.get("items", [])],
    }


def enrich_status(repo: str, number: int, curl: CurlFn = _curl) -> dict:
    """The full status signals for one PR (CI, review decision, conflicts, diffstat)."""
    pr = gh_json(f"repos/{repo}/pulls/{number}", curl)
    assert isinstance(pr, dict)
    sha = pr["head"]["sha"]
    check_runs = gh_json(f"repos/{repo}/commits/{sha}/check-runs", curl)
    combined = gh_json(f"repos/{repo}/commits/{sha}/status", curl)
    reviews = gh_json(f"repos/{repo}/pulls/{number}/reviews", curl)
    assert isinstance(check_runs, dict) and isinstance(combined, dict) and isinstance(reviews, list)
    return {
        "repo": repo,
        "number": number,
        "title": pr["title"],
        "body": pr.get("body") or "",
        "author": pr["user"]["login"],
        "state": "draft" if pr.get("draft") else "ready",
        "base": pr["base"]["ref"],
        "base_sha": pr["base"]["sha"],
        "head": pr["head"]["ref"],
        "head_sha": sha,
        "head_repo": (pr["head"].get("repo") or {}).get("full_name", repo),
        "created_at": pr["created_at"],
        "updated_at": pr["updated_at"],
        "ci": _ci_verdict(check_runs, combined),
        "review_decision": _review_decision(reviews),
        "has_conflicts": pr.get("mergeable_state") == "dirty",
        "mergeable_state": pr.get("mergeable_state"),
        "diffstat": {
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changed_files", 0),
            "commits": pr.get("commits", 0),
        },
        "comment_counts": {
            "general": pr.get("comments", 0),
            "line_level": pr.get("review_comments", 0),
            "reviews": len(reviews),
        },
        "url": pr["html_url"],
    }


def list_changed_files(repo: str, number: int, curl: CurlFn = _curl) -> list[dict]:
    """Changed files for a PR (paginated)."""
    files: list[dict] = []
    # GitHub caps PR files at 3000; 30 pages of 100 covers any PR.
    for page in range(1, 31):
        chunk = gh_json(f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}", curl)
        assert isinstance(chunk, list)
        if not chunk:
            break
        for entry in chunk:
            files.append({
                "path": entry["filename"],
                "previous_path": entry.get("previous_filename"),
                "status": entry["status"],
                "additions": entry.get("additions", 0),
                "deletions": entry.get("deletions", 0),
                "is_binary": entry.get("patch") is None and entry["status"] not in ("added", "removed"),
            })
        if len(chunk) < 100:
            break
    return files


# ---------------------------------------------------------------------------
# Conversation (read) + write-back (comments, reviews, edits)
# ---------------------------------------------------------------------------


def get_conversation(repo: str, number: int, curl: CurlFn = _curl) -> dict:
    """The PR's general comments, reviews, and line-level review comments."""
    issue_comments = gh_json(f"repos/{repo}/issues/{number}/comments?per_page=100", curl)
    reviews = gh_json(f"repos/{repo}/pulls/{number}/reviews?per_page=100", curl)
    review_comments = gh_json(f"repos/{repo}/pulls/{number}/comments?per_page=100", curl)
    assert isinstance(issue_comments, list) and isinstance(reviews, list) and isinstance(review_comments, list)

    def _user(obj: dict) -> str:
        return (obj.get("user") or {}).get("login", "?")

    return {
        "comments": [
            {"id": c["id"], "user": _user(c), "created_at": c["created_at"], "body": c.get("body") or "", "url": c.get("html_url")}
            for c in issue_comments
        ],
        "reviews": [
            {"id": r["id"], "user": _user(r), "state": r.get("state"), "submitted_at": r.get("submitted_at"), "body": r.get("body") or "", "url": r.get("html_url")}
            for r in reviews
            if r.get("state") != "PENDING"
        ],
        "review_comments": [
            {
                "id": rc["id"], "user": _user(rc), "path": rc["path"],
                "line": rc.get("line") or rc.get("original_line"),
                "side": rc.get("side", "RIGHT"), "body": rc.get("body") or "",
                "created_at": rc.get("created_at"), "url": rc.get("html_url"),
            }
            for rc in review_comments
        ],
    }


def add_issue_comment(repo: str, number: int, body: str, curl: CurlFn = _curl) -> dict:
    """Post a general (conversation) comment on the PR."""
    return gh_request("POST", f"repos/{repo}/issues/{number}/comments", {"body": body}, curl)


def delete_issue_comment(repo: str, comment_id: int, curl: CurlFn = _curl) -> None:
    """Delete a general comment (used for clean test round-trips)."""
    gh_request("DELETE", f"repos/{repo}/issues/comments/{comment_id}", curl=curl)


def update_pr(repo: str, number: int, fields: dict, curl: CurlFn = _curl) -> dict:
    """Edit the PR title and/or body."""
    allowed = {k: v for k, v in fields.items() if k in ("title", "body")}
    if not allowed:
        raise GitHubError("nothing to update (expected title and/or body)")
    return gh_request("PATCH", f"repos/{repo}/pulls/{number}", allowed, curl)


def create_review(
    repo: str, number: int, commit_id: str, body: str, event: str, comments: list[dict], curl: CurlFn = _curl
) -> dict:
    """Create a review. ``event`` is one of COMMENT / APPROVE / REQUEST_CHANGES,
    or empty/"PENDING_CREATE" to leave it pending (used for clean test round-trips).
    ``comments`` are ``{path, line, side, body}`` line-level comments.
    """
    payload: dict = {"commit_id": commit_id, "comments": comments}
    if body:
        payload["body"] = body
    if event and event != "PENDING_CREATE":
        payload["event"] = event
    return gh_request("POST", f"repos/{repo}/pulls/{number}/reviews", payload, curl)


def delete_pending_review(repo: str, number: int, review_id: int, curl: CurlFn = _curl) -> None:
    """Delete a still-pending review (used for clean test round-trips)."""
    gh_request("DELETE", f"repos/{repo}/pulls/{number}/reviews/{review_id}", curl=curl)


# ---------------------------------------------------------------------------
# Repo source-tree fetch + cache (the "auto-clone")
# ---------------------------------------------------------------------------


class RepoTree(NamedTuple):
    """An extracted source tree at a specific commit, on disk."""

    repo: str
    sha: str
    root: Path


def _safe_slug(repo: str) -> str:
    return repo.replace("/", "__")


def ensure_repo_tree(repo: str, sha: str, curl: CurlFn = _curl) -> RepoTree:
    """Fetch+extract the repo at ``sha`` (cached). Reuses existing GitHub auth.

    ``repo`` is the full ``owner/name`` of the repo that hosts the commit (for a
    PR this is the head repo, which may be a fork).
    """
    dest = REPO_CACHE / _safe_slug(repo) / sha
    marker = dest / ".extracted"
    if marker.exists():
        root = next(p for p in dest.iterdir() if p.is_dir())
        return RepoTree(repo=repo, sha=sha, root=root)

    dest.mkdir(parents=True, exist_ok=True)
    tarball = dest / "src.tar.gz"
    # The tarball endpoint 302-redirects to codeload; -L follows it with auth.
    raw = curl(["-L", f"{API}/repos/{repo}/tarball/{sha}"])
    tarball.write_bytes(raw)
    with tarfile.open(tarball, "r:gz") as tf:
        _safe_extract(tf, dest)
    tarball.unlink()
    marker.write_text(sha)
    root = next(p for p in dest.iterdir() if p.is_dir())
    return RepoTree(repo=repo, sha=sha, root=root)


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract guarding against path traversal outside ``dest``."""
    dest_resolved = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest_resolved)):
            raise GitHubError(f"unsafe path in tarball: {member.name}")
    # The explicit check above is the primary guard; the "data" filter is a
    # second line of defense (and silences the 3.14 default-filter warning).
    tf.extractall(dest, filter="data")


def read_tree_file(tree: RepoTree, rel_path: str) -> str | None:
    """Read a file from an extracted tree. None if missing or binary."""
    target = (tree.root / rel_path).resolve()
    if not str(target).startswith(str(tree.root.resolve())):
        raise GitHubError(f"path escapes tree: {rel_path}")
    if not target.is_file():
        return None
    data = target.read_bytes()
    if b"\x00" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace")


def list_tree_files(tree: RepoTree) -> list[str]:
    """All file paths in the tree, relative to its root (sorted)."""
    root = tree.root
    out: list[str] = []
    for path in root.rglob("*"):
        if path.is_file() and ".git/" not in str(path):
            out.append(str(path.relative_to(root)))
    out.sort()
    return out


_RG = shutil.which("rg") or "rg"
_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A line "looks like" a definition of ``sym`` when a definition keyword precedes
# it, or it is assigned/typed at the start of the line. Heuristic, language-
# agnostic -- used only to float likely definitions to the top of usage results.
_DEF_KEYWORDS = "def|class|func|fn|function|struct|interface|type|enum|impl|trait|const|let|var|module|package"


def _looks_like_def(text: str, symbol: str) -> bool:
    esc = re.escape(symbol)
    if re.search(rf"\b(?:{_DEF_KEYWORDS})\b[^=]*\b{esc}\b", text):
        return True
    if re.search(rf"#\s*define\s+{esc}\b", text):
        return True
    return bool(re.match(rf"\s*{esc}\s*[:=]", text))


def find_usages(tree: "RepoTree", symbol: str, limit: int = 400) -> dict:
    """Every whole-word occurrence of ``symbol`` in the tree, definitions first.

    Powered by ripgrep over the cached source -- language-agnostic find-usages
    plus a heuristic guess at which occurrences are the definition.
    """
    if not _SYMBOL_RE.fullmatch(symbol):
        raise GitHubError(f"invalid symbol: {symbol!r}")
    proc = subprocess.run(
        [_RG, "--json", "--word-regexp", "--fixed-strings", "--", symbol, "."],
        cwd=tree.root,
        capture_output=True,
        text=True,
        check=False,
    )
    # rg exits 1 when there are simply no matches -- not an error.
    if proc.returncode not in (0, 1):
        raise GitHubError(f"ripgrep failed: {proc.stderr[:300]}")
    results: list[dict] = []
    truncated = False
    for raw in proc.stdout.splitlines():
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj["data"]
        text = data["lines"]["text"].rstrip("\n")
        if "\x00" in text:  # binary line
            continue
        submatches = data.get("submatches") or [{"start": 0}]
        results.append({
            "path": data["path"]["text"],
            "line": data["line_number"],
            "col": submatches[0]["start"],
            "text": text[:240],
            "is_def": _looks_like_def(text, symbol),
        })
        if len(results) >= limit:
            truncated = True
            break
    results.sort(key=lambda r: (not r["is_def"], r["path"], r["line"]))
    return {
        "symbol": symbol,
        "total": len(results),
        "definitions": sum(1 for r in results if r["is_def"]),
        "truncated": truncated,
        "results": results,
    }


def get_file_at_ref(repo: str, path: str, ref: str, curl: CurlFn = _curl) -> str | None:
    """Base-version content of a file via the contents/blobs API. None if absent."""
    meta = gh_json(f"repos/{repo}/contents/{path}?ref={ref}", curl)
    if isinstance(meta, dict) and meta.get("message") == "Not Found":
        return None
    assert isinstance(meta, dict)
    content = meta.get("content")
    encoding = meta.get("encoding")
    if content and encoding == "base64":
        data = base64.b64decode(content)
        if b"\x00" in data[:8000]:
            return None
        return data.decode("utf-8", errors="replace")
    # Large files: contents API omits content; fall back to the blob by sha.
    sha = meta.get("sha")
    if sha:
        blob = gh_json(f"repos/{repo}/git/blobs/{sha}", curl)
        assert isinstance(blob, dict)
        if blob.get("encoding") == "base64" and blob.get("content"):
            data = base64.b64decode(blob["content"])
            if b"\x00" in data[:8000]:
                return None
            return data.decode("utf-8", errors="replace")
    return None
