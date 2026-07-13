"""Test utilities for the PR-review service.

The central helper is :class:`FakeCurl`, a drop-in for ``github._curl`` (the
``latchkey curl`` transport). Every network function in ``github`` takes a
``curl`` parameter; injecting a ``FakeCurl`` lets tests exercise the real
enrichment / diff / conversation / write-payload logic against canned responses
with no GitHub call and no live write ever happening.
"""

import io
import json
import tarfile
from pathlib import Path
from typing import NamedTuple

from pr_review import github, prepare
from pr_review.github import RepoTree


class FakeCurl(NamedTuple):
    """A routing stand-in for ``github._curl``.

    ``routes`` maps a substring of the request URL to the raw response bytes the
    transport should return for it; the route with the longest matching needle
    wins, so routing is independent of declaration order even when one needle is
    a prefix of another (e.g. ``/pulls/7`` vs ``/pulls/7/reviews``). A request
    whose URL matches no route raises -- so a test fails loudly if the code under
    test makes an unexpected call. ``calls`` records every argument list passed,
    so write tests can assert on the exact HTTP method, path, and JSON payload.

    Write requests (``gh_request`` builds them with ``-X``) get the HTTP-status
    marker appended to the body, mirroring what ``curl -w`` produces, so
    ``gh_request`` can parse the status it returns via ``status``.
    """

    routes: tuple[tuple[str, bytes], ...]
    calls: list[list[str]]
    status: int

    def __call__(self, args: list[str]) -> bytes:
        self.calls.append(list(args))
        url = args[-1]
        matches = [(needle, response) for needle, response in self.routes if needle in url]
        if not matches:
            raise AssertionError(f"FakeCurl: no route matched URL {url!r}")
        # Longest matching needle wins, so routing does not depend on the order
        # routes were declared in (one needle may be a prefix of another).
        body = max(matches, key=lambda item: len(item[0]))[1]
        if "-X" in args:
            return body + (github._STATUS_MARKER + str(self.status)).encode()
        return body


def make_curl(routes: dict[str, bytes], status: int = 200) -> FakeCurl:
    """Build a :class:`FakeCurl` with a fresh call log."""
    return FakeCurl(routes=tuple(routes.items()), calls=[], status=status)


def json_route(routes: dict[str, object], status: int = 200) -> FakeCurl:
    """Like :func:`make_curl` but each route value is JSON-encoded for you."""
    return make_curl({k: json.dumps(v).encode() for k, v in routes.items()}, status=status)


def parse_write_call(call: list[str]) -> dict:
    """Pull the HTTP method, URL, and decoded JSON payload out of a recorded
    ``gh_request`` argument list (the ones containing ``-X``)."""
    method = call[call.index("-X") + 1]
    url = call[-1]
    payload = None
    if "-d" in call:
        payload = json.loads(call[call.index("-d") + 1])
    return {"method": method, "url": url, "payload": payload}


def write_tree(root: Path, files: dict[str, str]) -> RepoTree:
    """Materialize ``files`` (relative path -> text) under ``root`` and wrap it
    as a :class:`RepoTree`, the shape the tree/usages/pyintel functions expect."""
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return RepoTree(repo="octocat/hello", sha="0" * 40, root=root)


def make_tarball_bytes(top_dir: str, files: dict[str, str]) -> bytes:
    """A gzip tarball whose entries all sit under ``top_dir`` -- the shape
    GitHub's tarball endpoint returns (a single top-level commit directory)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for rel, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=f"{top_dir}/{rel}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def make_evil_tarball_bytes() -> bytes:
    """A tarball with a path-traversal member, to exercise the extraction guard."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = b"pwned"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def seed_repo_cache(repo: str, sha: str, files: dict[str, str]) -> Path:
    """Pre-populate the on-disk repo cache (under the cwd) so ``ensure_repo_tree``
    treats the tree as already fetched and never calls the network. Returns the
    extracted source root. Call after ``chdir`` into the test's working dir."""
    dest = github.REPO_CACHE / github._safe_slug(repo) / sha
    root = dest / f"{repo.split('/')[-1]}-{sha[:7]}"
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    (dest / ".extracted").write_text(sha)
    return root


def seed_prepared_state(
    tree: RepoTree,
    roots: list[str],
    package_manager: str = "pnpm",
    notes: str = "used pnpm; engine-strict fallback",
) -> None:
    """Fake a completed rich-types install on ``tree`` without running the agent.

    Writes a ready ``.pr-review-prep`` sidecar (status + agent result + a
    typescript@5 stand-in) plus a ``node_modules`` under each project root, so the
    tree looks exactly like one the prepare agent finished -- enough for
    ``prepare._publish`` to capture and for reuse/auto-enable to materialize.
    """
    prepare._write_status(
        tree,
        {
            "state": "ready",
            "package_manager": package_manager,
            "roots": roots,
            "typescript_dir": prepare.PREP_DIRNAME,
        },
    )
    prepare._agent_result_path(tree).write_text(
        json.dumps({"package_manager": package_manager, "roots": roots, "notes": notes})
    )
    ts = tree.root / prepare.PREP_DIRNAME / "node_modules" / "typescript"
    ts.mkdir(parents=True)
    (ts / "package.json").write_text('{"name":"typescript","version":"5.4.0"}')
    (tree.root / prepare.PREP_DIRNAME / "package.json").write_text('{"dependencies":{"typescript":"5"}}')
    for root in roots:
        pkg = tree.root / root / "node_modules" / "left-pad"
        pkg.mkdir(parents=True)
        (pkg / "index.js").write_text("module.exports = 1;\n")
