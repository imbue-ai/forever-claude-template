"""Unit tests for pr_review.github.

The ``latchkey curl`` transport is always injected as a :class:`FakeCurl`, so no
test makes a real GitHub call or performs a real write. On-disk behavior (the
repo-tree cache, ripgrep find-usages, the path-traversal guards) runs for real
against trees built in ``tmp_path``.
"""

import base64
import json
from pathlib import Path

import pytest

from pr_review import github
from pr_review.testing import (
    json_route,
    make_curl,
    make_evil_tarball_bytes,
    make_tarball_bytes,
    parse_write_call,
    requires_ripgrep,
    seed_repo_cache,
    write_tree,
)

# --- gh_json / gh_request transport ---


def test_gh_json_parses_response() -> None:
    curl = json_route({"user": {"login": "octocat"}})
    assert github.gh_json("user", curl) == {"login": "octocat"}


def test_gh_json_raises_on_non_json() -> None:
    curl = make_curl({"user": b"<html>nope</html>"})
    with pytest.raises(github.GitHubError, match="non-JSON"):
        github.gh_json("user", curl)


def test_gh_request_returns_body_on_success() -> None:
    curl = json_route({"comments": {"id": 7}}, status=201)
    result = github.gh_request(
        "POST", "repos/o/r/issues/1/comments", {"body": "hi"}, curl
    )
    assert result == {"id": 7}


def test_gh_request_raises_on_error_status_with_api_message() -> None:
    curl = json_route({"pulls": {"message": "Validation Failed"}}, status=422)
    with pytest.raises(github.GitHubError, match="422.*Validation Failed"):
        github.gh_request("PATCH", "repos/o/r/pulls/1", {"title": "x"}, curl)


def test_gh_request_wraps_list_body() -> None:
    curl = make_curl({"x": b"[1, 2, 3]"}, status=200)
    assert github.gh_request("GET", "x", None, curl) == {"result": [1, 2, 3]}


# --- get_viewer ---


def test_get_viewer_returns_login() -> None:
    assert github.get_viewer(json_route({"user": {"login": "me"}})) == "me"


def test_get_viewer_raises_when_login_missing() -> None:
    with pytest.raises(github.GitHubError, match="viewer login"):
        github.get_viewer(json_route({"user": {}}))


# --- CI verdict (the deliberate combined-status fix) ---


def test_ci_verdict_failure_check_run_is_failing() -> None:
    check_runs = {"check_runs": [{"status": "completed", "conclusion": "failure"}]}
    out = github._ci_verdict(check_runs, {"state": "success", "total_count": 1})
    assert out["verdict"] == "failing"
    assert out["counts"]["failure"] == 1


def test_ci_verdict_pending_when_run_incomplete() -> None:
    check_runs = {"check_runs": [{"status": "in_progress"}]}
    assert github._ci_verdict(check_runs, {})["verdict"] == "pending"


def test_ci_verdict_passing_when_only_successes() -> None:
    check_runs = {"check_runs": [{"status": "completed", "conclusion": "success"}]}
    assert github._ci_verdict(check_runs, {})["verdict"] == "passing"


def test_ci_verdict_empty_combined_status_does_not_override_clean_checks() -> None:
    # The legacy combined-status endpoint returns state="pending" with zero
    # statuses by default; it must not turn a clean check-runs result pending.
    check_runs = {"check_runs": [{"status": "completed", "conclusion": "success"}]}
    combined = {"state": "pending", "total_count": 0}
    assert github._ci_verdict(check_runs, combined)["verdict"] == "passing"


def test_ci_verdict_combined_failure_counts_when_statuses_exist() -> None:
    combined = {"state": "failure", "total_count": 2}
    assert github._ci_verdict({"check_runs": []}, combined)["verdict"] == "failing"


def test_ci_verdict_none_when_nothing_reported() -> None:
    assert (
        github._ci_verdict({"check_runs": []}, {"total_count": 0})["verdict"] == "none"
    )


def test_ci_verdict_neutral_conclusion_counts_as_neutral_not_failing() -> None:
    check_runs = {"check_runs": [{"status": "completed", "conclusion": "skipped"}]}
    out = github._ci_verdict(check_runs, {})
    assert out["verdict"] == "none"
    assert out["counts"]["neutral"] == 1


# --- review decision ---


def test_review_decision_changes_requested_wins() -> None:
    reviews = [
        {"state": "APPROVED", "user": {"login": "a"}},
        {"state": "CHANGES_REQUESTED", "user": {"login": "b"}},
    ]
    assert github._review_decision(reviews) == "changes requested"


def test_review_decision_latest_state_per_user() -> None:
    # One reviewer who approved then later requested changes counts as changes.
    reviews = [
        {"state": "APPROVED", "user": {"login": "a"}},
        {"state": "CHANGES_REQUESTED", "user": {"login": "a"}},
    ]
    assert github._review_decision(reviews) == "changes requested"


def test_review_decision_approved() -> None:
    assert (
        github._review_decision([{"state": "APPROVED", "user": {"login": "a"}}])
        == "approved"
    )


def test_review_decision_commented_only() -> None:
    assert (
        github._review_decision([{"state": "COMMENTED", "user": {"login": "a"}}])
        == "commented"
    )


def test_review_decision_none_when_empty() -> None:
    assert github._review_decision([]) == "none"


# --- search-item summary ---


def test_summarize_search_item_builds_row() -> None:
    item = {
        "repository_url": f"{github.API}/repos/octocat/hello",
        "number": 42,
        "title": "Add feature",
        "user": {"login": "octocat"},
        "draft": True,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "comments": 3,
        "html_url": "https://github.com/octocat/hello/pull/42",
    }
    row = github._summarize_search_item(item, viewer="octocat")
    assert row["repo"] == "octocat/hello"
    assert row["number"] == 42
    assert row["is_mine"] is True
    assert row["state"] == "draft"
    assert row["comments"] == 3


def test_summarize_search_item_not_mine_and_ready() -> None:
    item = {
        "repository_url": f"{github.API}/repos/octocat/hello",
        "number": 1,
        "title": "t",
        "user": {"login": "someone"},
    }
    row = github._summarize_search_item(item, viewer="me")
    assert row["is_mine"] is False
    assert row["state"] == "ready"


# --- list_prs ---


def test_list_prs_returns_both_buckets() -> None:
    authored = {
        "items": [
            {
                "repository_url": f"{github.API}/repos/o/a",
                "number": 1,
                "title": "mine",
                "user": {"login": "me"},
            }
        ]
    }
    requested = {
        "items": [
            {
                "repository_url": f"{github.API}/repos/o/b",
                "number": 2,
                "title": "theirs",
                "user": {"login": "you"},
            }
        ]
    }
    curl = make_curl(
        {
            "author:me": json.dumps(authored).encode(),
            "review-requested:me": json.dumps(requested).encode(),
        }
    )
    out = github.list_prs("me", curl)
    assert out["viewer"] == "me"
    assert [p["number"] for p in out["authored"]] == [1]
    assert [p["number"] for p in out["review_requested"]] == [2]


# --- enrich_status ---


def _pr_payload() -> dict:
    return {
        "title": "My PR",
        "body": "Body text",
        "user": {"login": "octocat"},
        "draft": False,
        "base": {"ref": "main", "sha": "basesha"},
        "head": {
            "ref": "feature",
            "sha": "headsha",
            "repo": {"full_name": "octocat/hello"},
        },
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "mergeable_state": "dirty",
        "additions": 10,
        "deletions": 2,
        "changed_files": 3,
        "commits": 1,
        "comments": 4,
        "review_comments": 5,
        "html_url": "https://github.com/octocat/hello/pull/7",
    }


def test_enrich_status_assembles_all_signals() -> None:
    curl = make_curl(
        {
            "/pulls/7/reviews": json.dumps(
                [{"state": "APPROVED", "user": {"login": "rev"}}]
            ).encode(),
            "/pulls/7": json.dumps(_pr_payload()).encode(),
            "/check-runs": json.dumps(
                {"check_runs": [{"status": "completed", "conclusion": "success"}]}
            ).encode(),
            "/status": json.dumps({"state": "success", "total_count": 0}).encode(),
        }
    )
    out = github.enrich_status("octocat/hello", 7, curl)
    assert out["ci"]["verdict"] == "passing"
    assert out["review_decision"] == "approved"
    assert out["has_conflicts"] is True
    assert out["head_sha"] == "headsha"
    assert out["head_repo"] == "octocat/hello"
    assert out["diffstat"] == {
        "additions": 10,
        "deletions": 2,
        "changed_files": 3,
        "commits": 1,
    }
    assert out["comment_counts"] == {"general": 4, "line_level": 5, "reviews": 1}


def test_enrich_status_head_repo_falls_back_to_repo_when_fork_repo_missing() -> None:
    payload = _pr_payload()
    payload["head"]["repo"] = None
    curl = make_curl(
        {
            "/pulls/7/reviews": b"[]",
            "/pulls/7": json.dumps(payload).encode(),
            "/check-runs": json.dumps({"check_runs": []}).encode(),
            "/status": json.dumps({"total_count": 0}).encode(),
        }
    )
    out = github.enrich_status("octocat/hello", 7, curl)
    assert out["head_repo"] == "octocat/hello"


# --- list_changed_files ---


def test_list_changed_files_maps_entries_and_detects_binary_and_rename() -> None:
    page = [
        {
            "filename": "a.py",
            "status": "modified",
            "additions": 1,
            "deletions": 1,
            "patch": "@@",
        },
        {"filename": "img.png", "status": "modified"},  # no patch -> binary
        {
            "filename": "new.py",
            "previous_filename": "old.py",
            "status": "renamed",
            "patch": "@@",
        },
    ]
    curl = make_curl({"page=1": json.dumps(page).encode(), "page=2": b"[]"})
    files = github.list_changed_files("o/r", 1, curl)
    assert [f["path"] for f in files] == ["a.py", "img.png", "new.py"]
    assert files[1]["is_binary"] is True
    assert files[0]["is_binary"] is False
    assert files[2]["previous_path"] == "old.py"


def test_list_changed_files_stops_on_short_page() -> None:
    curl = make_curl(
        {"page=1": json.dumps([{"filename": "a", "status": "added"}]).encode()}
    )
    files = github.list_changed_files("o/r", 1, curl)
    assert len(files) == 1
    # Only page 1 should have been requested (short page ends pagination).
    assert all("page=1" in c[-1] for c in curl.calls)


# --- get_conversation ---


def test_get_conversation_maps_and_filters_pending_reviews() -> None:
    issue_comments = [
        {
            "id": 1,
            "user": {"login": "a"},
            "created_at": "t",
            "body": "hi",
            "html_url": "u1",
        }
    ]
    reviews = [
        {
            "id": 2,
            "user": {"login": "b"},
            "state": "APPROVED",
            "submitted_at": "t",
            "body": "lgtm",
            "html_url": "u2",
        },
        {"id": 3, "user": {"login": "c"}, "state": "PENDING", "body": "wip"},
    ]
    review_comments = [
        {
            "id": 4,
            "user": {"login": "d"},
            "path": "f.py",
            "original_line": 12,
            "body": "nit",
            "created_at": "t",
            "html_url": "u4",
            "diff_hunk": "@@ -1,2 +1,2 @@\n-old\n+new",
            "in_reply_to_id": None,
        },
        {
            "id": 5,
            "user": {"login": "e"},
            "path": "f.py",
            "line": 12,
            "body": "reply",
            "created_at": "t2",
            "html_url": "u5",
            "in_reply_to_id": 4,
        },
    ]
    curl = make_curl(
        {
            "/issues/1/comments": json.dumps(issue_comments).encode(),
            "/pulls/1/reviews": json.dumps(reviews).encode(),
            "/pulls/1/comments": json.dumps(review_comments).encode(),
        }
    )
    out = github.get_conversation("o/r", 1, curl)
    assert [c["id"] for c in out["comments"]] == [1]
    assert [r["id"] for r in out["reviews"]] == [2]  # PENDING filtered out
    assert out["review_comments"][0]["line"] == 12  # falls back to original_line
    assert out["review_comments"][0]["side"] == "RIGHT"
    assert out["review_comments"][0]["diff_hunk"] == "@@ -1,2 +1,2 @@\n-old\n+new"
    assert (
        out["review_comments"][1]["in_reply_to_id"] == 4
    )  # reply linked to its thread root


# --- write-payload construction (no real writes) ---


def test_add_issue_comment_builds_post_payload() -> None:
    curl = json_route({"comments": {"id": 99}}, status=201)
    github.add_issue_comment("o/r", 5, "Nice work", curl)
    parsed = parse_write_call(curl.calls[0])
    assert parsed["method"] == "POST"
    assert parsed["url"].endswith("repos/o/r/issues/5/comments")
    assert parsed["payload"] == {"body": "Nice work"}


def test_update_pr_filters_to_allowed_fields() -> None:
    curl = json_route({"pulls": {"number": 5}})
    github.update_pr(
        "o/r", 5, {"title": "New", "body": "Desc", "state": "closed"}, curl
    )
    parsed = parse_write_call(curl.calls[0])
    assert parsed["method"] == "PATCH"
    assert parsed["payload"] == {"title": "New", "body": "Desc"}


def test_update_pr_raises_when_no_allowed_fields() -> None:
    with pytest.raises(github.GitHubError, match="nothing to update"):
        github.update_pr("o/r", 5, {"state": "closed"}, json_route({}))


def test_create_review_includes_body_and_event() -> None:
    curl = json_route({"reviews": {"id": 1}})
    comments = [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "fix"}]
    github.create_review("o/r", 5, "sha123", "Overall good", "APPROVE", comments, curl)
    parsed = parse_write_call(curl.calls[0])
    assert parsed["method"] == "POST"
    assert parsed["url"].endswith("repos/o/r/pulls/5/reviews")
    assert parsed["payload"] == {
        "commit_id": "sha123",
        "comments": comments,
        "body": "Overall good",
        "event": "APPROVE",
    }


def test_create_review_omits_event_when_pending() -> None:
    curl = json_route({"reviews": {"id": 1}})
    github.create_review("o/r", 5, "sha123", "", "PENDING_CREATE", [], curl)
    parsed = parse_write_call(curl.calls[0])
    assert parsed["payload"] == {"commit_id": "sha123", "comments": []}


def test_set_pr_state_closes_pr() -> None:
    curl = json_route({"pulls": {"number": 5, "state": "closed"}})
    github.set_pr_state("o/r", 5, "closed", curl)
    parsed = parse_write_call(curl.calls[0])
    assert parsed["method"] == "PATCH"
    assert parsed["url"].endswith("repos/o/r/pulls/5")
    assert parsed["payload"] == {"state": "closed"}


def test_set_pr_state_rejects_invalid_state() -> None:
    with pytest.raises(github.GitHubError, match="invalid state"):
        github.set_pr_state("o/r", 5, "merged", json_route({}))


def test_merge_pr_builds_put_with_method() -> None:
    curl = json_route(
        {"merge": {"merged": True, "message": "Pull Request successfully merged"}}
    )
    github.merge_pr("o/r", 5, "squash", curl)
    parsed = parse_write_call(curl.calls[0])
    assert parsed["method"] == "PUT"
    assert parsed["url"].endswith("repos/o/r/pulls/5/merge")
    assert parsed["payload"] == {"merge_method": "squash"}


def test_merge_pr_rejects_invalid_method() -> None:
    with pytest.raises(github.GitHubError, match="invalid merge method"):
        github.merge_pr("o/r", 5, "fast-forward", json_route({}))


def test_delete_issue_comment_issues_delete() -> None:
    curl = make_curl({"issues/comments/8": b""}, status=204)
    github.delete_issue_comment("o/r", 8, curl)
    parsed = parse_write_call(curl.calls[0])
    assert parsed["method"] == "DELETE"
    assert parsed["url"].endswith("repos/o/r/issues/comments/8")


def test_delete_pending_review_issues_delete() -> None:
    curl = make_curl({"pulls/1/reviews/9": b""}, status=204)
    github.delete_pending_review("o/r", 1, 9, curl)
    parsed = parse_write_call(curl.calls[0])
    assert parsed["method"] == "DELETE"
    assert parsed["url"].endswith("repos/o/r/pulls/1/reviews/9")


# --- get_file_at_ref ---


def test_get_file_at_ref_decodes_base64_content() -> None:
    encoded = base64.b64encode(b"print('hi')\n").decode()
    curl = json_route({"/contents/a.py": {"content": encoded, "encoding": "base64"}})
    assert github.get_file_at_ref("o/r", "a.py", "main", curl) == "print('hi')\n"


def test_get_file_at_ref_returns_none_when_not_found() -> None:
    curl = json_route({"/contents/missing.py": {"message": "Not Found"}})
    assert github.get_file_at_ref("o/r", "missing.py", "main", curl) is None


def test_get_file_at_ref_returns_none_for_binary() -> None:
    encoded = base64.b64encode(b"\x00\x01\x02binary").decode()
    curl = json_route({"/contents/x.bin": {"content": encoded, "encoding": "base64"}})
    assert github.get_file_at_ref("o/r", "x.bin", "main", curl) is None


def test_get_file_at_ref_falls_back_to_blob_for_large_file() -> None:
    blob_content = base64.b64encode(b"big file body\n").decode()
    curl = make_curl(
        {
            "/contents/big.py": json.dumps(
                {"sha": "blobsha", "encoding": "none"}
            ).encode(),
            "/git/blobs/blobsha": json.dumps(
                {"encoding": "base64", "content": blob_content}
            ).encode(),
        }
    )
    assert github.get_file_at_ref("o/r", "big.py", "main", curl) == "big file body\n"


# --- repo-tree cache + extraction ---


def test_ensure_repo_tree_uses_cache_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    seed_repo_cache("octocat/hello", "a" * 40, {"main.py": "x = 1\n"})

    def explode(_args: list[str]) -> bytes:
        raise AssertionError("network must not be called when cache is warm")

    tree = github.ensure_repo_tree("octocat/hello", "a" * 40, explode)
    assert (tree.root / "main.py").read_text() == "x = 1\n"


def test_ensure_repo_tree_fetches_and_extracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    tarball = make_tarball_bytes(
        "hello-abcdef0", {"src/app.py": "print(1)\n", "README.md": "hi\n"}
    )
    curl = make_curl({"/tarball/": tarball})
    tree = github.ensure_repo_tree("octocat/hello", "abcdef0", curl)
    assert (tree.root / "src" / "app.py").read_text() == "print(1)\n"
    # The cache marker is written so a second call skips the network.
    assert (github.REPO_CACHE / "octocat__hello" / "abcdef0" / ".extracted").exists()


def test_ensure_repo_tree_rejects_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    curl = make_curl({"/tarball/": make_evil_tarball_bytes()})
    with pytest.raises(github.GitHubError, match="unsafe path"):
        github.ensure_repo_tree("octocat/hello", "evil", curl)


# --- read_tree_file / list_tree_files ---


def test_read_tree_file_reads_text(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"a.py": "hello\n"})
    assert github.read_tree_file(tree, "a.py") == "hello\n"


def test_read_tree_file_missing_returns_none(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"a.py": "x\n"})
    assert github.read_tree_file(tree, "nope.py") is None


def test_read_tree_file_binary_returns_none(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {})
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02")
    assert github.read_tree_file(tree, "b.bin") is None


def test_read_tree_file_rejects_escape(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"a.py": "x\n"})
    with pytest.raises(github.GitHubError, match="escapes tree"):
        github.read_tree_file(tree, "../outside.py")


def test_list_tree_files_sorted_and_excludes_git(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"z.py": "1", "a/b.py": "2", ".git/config": "3"})
    assert github.list_tree_files(tree) == ["a/b.py", "z.py"]


# --- find_usages (real ripgrep) ---


@requires_ripgrep
def test_find_usages_finds_occurrences_definitions_first(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {
            "defs.py": "def widget():\n    return 1\n",
            "use.py": "from defs import widget\nwidget()\n",
        },
    )
    out = github.find_usages(tree, "widget")
    assert out["symbol"] == "widget"
    assert out["total"] == 3
    assert out["definitions"] >= 1
    # The first result is flagged as the definition and sorts to the top.
    assert out["results"][0]["is_def"] is True


@requires_ripgrep
def test_find_usages_no_matches_is_empty_not_error(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"a.py": "x = 1\n"})
    out = github.find_usages(tree, "nonexistent")
    assert out["total"] == 0
    assert out["results"] == []


def test_find_usages_rejects_invalid_symbol(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"a.py": "x = 1\n"})
    with pytest.raises(github.GitHubError, match="invalid symbol"):
        github.find_usages(tree, "not a symbol")


@requires_ripgrep
def test_find_usages_truncates_at_limit(tmp_path: Path) -> None:
    body = "\n".join(f"thing_{i} = thing  # thing" for i in range(50))
    tree = write_tree(tmp_path, {"a.py": "thing = 1\n" + body + "\n"})
    out = github.find_usages(tree, "thing", limit=5)
    assert out["truncated"] is True
    assert out["total"] == 5
