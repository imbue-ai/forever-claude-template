"""Shared test fakes for the minds_workspace_server package.

Houses deterministic stand-ins for outside-world dependencies that the
`claude_auth` module exposes via injectable module-level callables
(`command_runner`, `pexpect_spawner`). Both `claude_auth_test.py` and
`claude_auth_endpoints_test.py` need the same fakes, so they live here
rather than being copy-pasted into each test module.
"""

from __future__ import annotations

import re


class FakeFinishedProcess:
    """Minimal stand-in for a `FinishedProcess` returned by `command_runner`.

    The real subprocess runner produces an object with `stdout`, `stderr`,
    and `returncode`; this class exposes just those three so tests can
    drive every branch the `claude_auth` callers care about.
    """

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakePexpectProcess:
    """Records the inputs the OAuth flow sends to a `pexpect.spawn`.

    Constructor arguments parameterize how the fake responds to `expect()`:

    - `url_match`: when non-None, the first `expect()` returns
      `expect_return_index` (default 0 for the URL-matched branch) and
      `self.match` is preset to the result of regex-matching `url_match`.
      When None, the first `expect()` returns `expect_return_index`
      (typically 1 for EOF or 2 for TIMEOUT) without setting `match`.
    - `expect_return_index`: index returned on the first `expect()` call.
      Lets a test simulate the URL-found / EOF-before-URL / timeout
      branches of `_spawn_oauth_and_parse_url`.
    - `eof_return_index`: index returned on every subsequent `expect()`
      call. Defaults to 0 (the EOF branch in `_drive_oauth_code`'s
      `[pexpect.EOF, pexpect.TIMEOUT]` pattern) so the post-code-submit
      teardown lands in the success path.
    """

    def __init__(
        self,
        url_match: str | None = None,
        expect_return_index: int = 0,
        eof_return_index: int = 0,
    ) -> None:
        self._expect_return_index = expect_return_index
        self._eof_return_index = eof_return_index
        self._expect_call_count = 0
        self.sendline_calls: list[str] = []
        self.terminate_calls = 0
        self.close_calls = 0
        self.timeout: float | None = None
        self.match: re.Match[str] | None = None
        if url_match is not None:
            self.match = re.compile(r".*").match(url_match)
            assert self.match is not None

    def expect(self, _patterns: object) -> int:
        self._expect_call_count += 1
        if self._expect_call_count == 1:
            return self._expect_return_index
        return self._eof_return_index

    def sendline(self, s: str) -> None:
        self.sendline_calls.append(s)

    def isalive(self) -> bool:
        return True

    def terminate(self, force: bool = False) -> None:
        self.terminate_calls += 1

    def close(self) -> None:
        self.close_calls += 1
