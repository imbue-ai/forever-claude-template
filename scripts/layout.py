#!/usr/bin/env python3
"""Agent-facing helper for inspecting and mutating the workspace dockview layout.

Subcommands:
    list                                List addressable services + agents (open/running flags).
    inspect                             Describe the live dockview state (compact by default; --verbose for YAML tree).
    where <ref-or-service>              Show one panel: its group's tab-mates and the refs in each cardinal direction.
    open <ref-or-service>               Surface a service (focus-if-open, else tab into / split next to caller's chat).
    focus <ref-or-service>              Activate the named panel within its group.
    split <ref-or-service> [...]        Add a panel relative to another panel; tabs into an existing adjacent group by default.
    close <ref-or-service>              Remove the named panel.
    move <ref-or-service> --relative-to <ref-or-service> [...]  Relocate a panel; iframe DOM is preserved.
    rename <ref-or-service> <title>     Update the panel's tab title.
    maximize <ref-or-service>           Maximize the panel's group within the dockview.
    restore                             Exit a maximized group.
    replace-url <ref-or-service> <url>  Swap an iframe's src (service:<name>[/<path>] or https://...).
    refresh <ref-or-service>            Reload one iframe; ``service:<name>`` reloads all iframes for that service.

Every ref-accepting argument (positional ref, ``--relative-to``) accepts a bare
service name as shorthand for ``service:<name>``. ``open`` and ``split`` also
accept an external ``https://`` URL (bare, or with an optional ``url:`` prefix)
as the panel to create -- it surfaces as an ad-hoc URL tab.

``--direction`` on ``split`` / ``move`` accepts five values:

- ``left`` / ``right`` / ``above`` / ``below`` -- target the *adjacent*
  group in that cardinal direction relative to the anchor. By default,
  tabs into an existing group that already lives there; ``--new-group``
  forces a fresh column / row instead.
- ``within`` -- target the *anchor's own* group, tabbing the panel in
  alongside it. ``--new-group`` is meaningless with ``within`` and is
  rejected. This is the single-call form of "put this in the same group
  as that".

Mutating ops (``open`` / ``split`` / ``move`` / ``focus`` / ``close`` /
``rename`` / ``maximize`` / ``restore`` / ``replace-url`` / ``refresh``)
wait for the resulting state to be observable via ``inspect`` before
returning. On success they print a concise diff on stderr; on a no-op
(the requested end state already holds) they print
``no change: <ref> is ...`` to stderr and exit 0. ``maximize`` /
``restore`` / ``refresh`` have no observable layout-state change, so
they confirm the broadcast was sent and note that explicitly.

All ops POST a single body ``{op, args, agent_id}`` to a loopback-only endpoint
on the workspace server. The caller's ``MNGR_AGENT_ID`` is sent both in the
JSON body and as the ``X-Mngr-Agent-Id`` request header for telemetry.

Output for ``list`` is YAML by default. ``inspect`` and ``where`` default to a
compact human-scannable rendering; pass ``--verbose`` for the full YAML tree
including ``panel_id`` / URL details. Pass ``--json`` to either for the raw
structured object.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import tomlkit
import yaml

DEFAULT_APPLICATIONS_FILE = "runtime/applications.toml"
ENV_APPLICATIONS_FILE = "MINDS_APPLICATIONS_FILE"
DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"
MNGR_AGENT_ID_HEADER = "X-Mngr-Agent-Id"
# Escape hatch for environments without a live frontend to apply layout
# ops (e.g. the acceptance test that exercises the broadcast pipeline
# but has no DOM). When set to any non-empty value, mutating ops skip
# the wait-stable poll, the diff print, and no-op detection -- the
# script returns as soon as the HTTP POST succeeds. Production callers
# should never set this; the wait-stable contract is the whole point.
ENV_NO_WAIT_STABLE = "MINDS_LAYOUT_NO_WAIT_STABLE"

# How long ``open`` / ``split`` wait for a freshly-registered service to
# appear before giving up. The supervisord-managed forward_port.py call races
# with the agent invoking this script right after build-web-service, so we
# tolerate a brief window where the entry is not yet visible.
_REGISTRATION_TIMEOUT_SECONDS = 5.0
_REGISTRATION_POLL_INTERVAL_SECONDS = 0.25

# Set of accepted ref prefixes.
#
# ``chat-terminal:<name>`` addresses the singleton terminal panel attached
# to the named agent's tmux session (URL pattern ``/service/terminal/
# ?arg=_&arg=agent&arg=<name>``). Listed before ``chat:`` because the
# ``_normalize_ref`` prefix scan returns on the first match -- if ``chat:``
# came first the longer prefix would never be recognized and
# ``chat-terminal:foo`` would degrade to a bare service-name fallback.
_REF_PREFIXES = (
    "service:",
    "chat-terminal:",
    "chat:",
    "terminal:",
    "url:",
    "subagent:",
)
# Set of accepted directions for split/move. ``within`` is the synthetic
# direction that means "tab into the anchor's own group" -- the four
# cardinal values all describe *adjacent* groups.
_WITHIN_DIRECTION = "within"
_CARDINAL_DIRECTIONS = ("left", "right", "above", "below")
_DIRECTIONS = (*_CARDINAL_DIRECTIONS, _WITHIN_DIRECTION)

# How long mutating ops wait for the resulting state to show up in
# ``inspect`` before declaring a timeout. The frontend autosaves with a
# 1.5 s debounce, so a few seconds of headroom is enough for the
# broadcast -> apply -> debounced save cycle to land on disk.
_WAIT_STABLE_CAP_SECONDS = 5.0
_WAIT_STABLE_POLL_SECONDS = 0.25


# Exit codes -- intentionally minimal: 0 / 1 / 3.
# Agents typically branch on "did it work" (0 vs anything else); the only
# distinct code worth its own slot is contention, which is the one error
# class where retry-with-backoff is the right response. Everything else
# (network, not-registered, not-found, bad-request, generic HTTP) folds
# into ``EXIT_ERROR``; the agent gets the specific reason from stderr.
# Slot 2 is left alone to avoid colliding with argparse's CLI-usage exit.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 3


def _workspace_base_url() -> str:
    return os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL).rstrip("/")


def _mngr_agent_id() -> str:
    return os.environ.get(ENV_MNGR_AGENT_ID, "")


def _applications_file() -> Path:
    """Path to the agent's applications.toml registry.

    Defaults to ``runtime/applications.toml`` relative to cwd (the script
    is invoked from the agent's repo root). Override via
    ``MINDS_APPLICATIONS_FILE`` -- used by tests to point at a sandboxed
    fixture without depending on cwd.
    """
    return Path(os.environ.get(ENV_APPLICATIONS_FILE, DEFAULT_APPLICATIONS_FILE))


def _read_application_names(path: Path) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        doc = tomlkit.load(f)
    apps = doc.get("applications", [])
    names: list[str] = []
    for app in apps:
        name = app.get("name") if hasattr(app, "get") else None
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _is_service_registered(name: str) -> bool:
    return name in _read_application_names(_applications_file())


def _wait_for_registration(name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _is_service_registered(name):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_REGISTRATION_POLL_INTERVAL_SECONDS)


# Sentinel ref the frontend resolves to the caller's own chat panel. Valid as
# a ``--relative-to`` value for ``split`` / ``move`` and as a target for any
# op (e.g. ``focus self``).
_SELF_REF = "self"


def _normalize_ref(value: str) -> str:
    """Expand bare service-name shorthand into a full ``service:`` ref.

    Anything that already carries a known prefix is returned as-is, and the
    ``self`` sentinel (resolved frontend-side to the caller's chat panel)
    is preserved verbatim. An external ``https://`` URL -- bare or written
    with the optional ``url:`` alias -- normalizes to the bare URL, which
    the frontend treats as a creation ref for an ad-hoc URL panel. (The
    ``url:<hash>`` form, which addresses an already-open URL panel, is
    left untouched.)
    """
    if value == _SELF_REF:
        return value
    if value.startswith("https://"):
        return value
    if value.startswith("url:https://"):
        return value.removeprefix("url:")
    for prefix in _REF_PREFIXES:
        if value.startswith(prefix):
            return value
    return f"service:{value}"


def _validate_ref(ref: str) -> None:
    """Raise SystemExit if the ref carries an unknown prefix or empty name.

    The ``self`` sentinel and bare ``https://`` external URLs are accepted
    in addition to the prefix forms. Empty-name forms like ``chat:`` or
    ``service:`` would otherwise sail past the prefix check and round-trip
    through the broadcast pipeline before timing out with no useful error.
    """
    if ref == _SELF_REF:
        return
    if ref.startswith("https://"):
        return
    if not any(ref.startswith(p) for p in _REF_PREFIXES):
        sys.stderr.write(
            f"error: ref {ref!r} must start with one of {_REF_PREFIXES}, "
            f"be a bare service name, or be an https:// URL\n"
        )
        raise SystemExit(EXIT_ERROR)
    prefix, _, name = ref.partition(":")
    if not name:
        sys.stderr.write(
            f"error: ref {ref!r} is missing a name after {prefix + ':'!r}\n"
        )
        raise SystemExit(EXIT_ERROR)


def _validate_replace_url(url: str) -> None:
    """Reject anything that isn't ``service:<name>[/<path>]`` or ``https://...``."""
    if url.startswith("service:") or url.startswith("https://"):
        return
    sys.stderr.write(
        f"error: replace-url accepts only ``service:<name>[/<path>]`` shorthand or a full https:// URL (got {url!r})\n"
    )
    raise SystemExit(EXIT_ERROR)


def _resolve_replace_url(url: str) -> str:
    """Project a ``replace-url`` URL argument to the form stored on the panel.

    Mirrors the frontend's ``resolveReplaceUrl`` (in ``DockviewWorkspace.ts``):
    ``service:<name>`` becomes ``/service/<name>/`` (matching ``getServiceUrl``),
    ``service:<name>/<path>`` becomes ``/service/<name>/<path>``, and plain
    ``https://`` URLs pass through unchanged. The wait-stable predicate for
    ``replace-url`` compares against the panel's stored URL, so it must use
    this resolved form rather than the literal shorthand the agent typed --
    otherwise every ``service:`` replace-url times out waiting for a string
    that will never appear.
    """
    if not url.startswith("service:"):
        return url
    remainder = url.removeprefix("service:")
    slash_index = remainder.find("/")
    if slash_index == -1:
        return f"/service/{remainder}/"
    name = remainder[:slash_index]
    path = remainder[slash_index + 1 :]
    return f"/service/{name}/{path}"


def _post_layout(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
    """POST {op, args, agent_id} to /api/layout/broadcast and return (status, parsed_or_raw)."""
    url = f"{_workspace_base_url()}/api/layout/broadcast"
    body = json.dumps({"op": op, "args": args, "agent_id": _mngr_agent_id()}).encode(
        "utf-8"
    )
    headers = {
        "Content-Type": "application/json",
        MNGR_AGENT_ID_HEADER: _mngr_agent_id(),
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, _maybe_parse_json(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return e.code, _maybe_parse_json(raw)
    except urllib.error.URLError as e:
        return -1, str(e.reason)


def _maybe_parse_json(text: str) -> dict[str, Any] | str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict):
        return parsed
    return text


def _report_failure(op: str, status: int, body: dict[str, Any] | str) -> int:
    """Translate (status, body) into a stderr message + exit code.

    Everything except mutex contention exits 1; the specific reason is in
    the stderr message. ``EXIT_CONFLICT`` (3) stays distinct because
    retry-with-backoff is the right response, and wrapper scripts need to
    branch on it.
    """
    if status == -1:
        sys.stderr.write(f"error: could not reach workspace server: {body}\n")
        return EXIT_ERROR
    detail: str = ""
    if isinstance(body, dict):
        detail = str(body.get("detail", body))
        if status == 409:
            in_flight = body.get("in_flight") or {}
            retry_ms = body.get("retry_after_ms")
            sys.stderr.write(
                f"error: layout op {op!r} rejected (HTTP 409 conflict): {detail}\n"
                f"  in-flight: agent_id={in_flight.get('agent_id')} op={in_flight.get('operation')} "
                f"args={in_flight.get('args')} started_at={in_flight.get('started_at')}\n"
                f"  retry_after_ms={retry_ms}\n"
            )
            return EXIT_CONFLICT
        if status == 404:
            sys.stderr.write(
                f"error: layout op {op!r} target not found (HTTP 404): {detail}\n"
            )
            return EXIT_ERROR
        if status == 400:
            sys.stderr.write(f"error: layout op {op!r} rejected (HTTP 400): {detail}\n")
            return EXIT_ERROR
    else:
        detail = body
    sys.stderr.write(f"error: layout op {op!r} failed (HTTP {status}): {detail}\n")
    return EXIT_ERROR


def _emit_allocated_ref(body: dict[str, Any] | str) -> None:
    """Print the server-allocated ref to stdout when the response includes one.

    Today this only fires for ``open`` / ``split`` targeting
    ``service:terminal``: the server pre-mints the panel id (mirroring the
    UI's "New terminal" button, which creates a fresh tab on every click)
    and returns the resulting ``terminal:<hash>`` ref so callers can capture
    it for later ops without round-tripping through ``inspect``.
    """
    if isinstance(body, dict):
        ref = body.get("ref")
        if isinstance(ref, str) and ref:
            sys.stdout.write(ref)
            sys.stdout.write("\n")


def _emit_structured(data: Any, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(data, indent=2))
        sys.stdout.write("\n")
    else:
        # ``sort_keys=False`` so the YAML preserves the server's intentional
        # ordering (e.g. tree-before-flat-panels, panels in tab order).
        yaml.safe_dump(data, sys.stdout, sort_keys=False, default_flow_style=False)


# ---------- Inspect helpers (used by wait-stable, diff, where, compact view) ----------


def _fetch_layout() -> dict[str, Any] | None:
    """Run ``inspect`` once and return the parsed ``layout`` block, or None on failure.

    Used by ``_wait_stable``, the diff printer, and the ``where`` command.
    A None return means the inspect HTTP call failed -- callers treat this
    as "state unknown" rather than "layout is empty".
    """
    status, body = _post_layout("inspect", {})
    if status != 200 or not isinstance(body, dict):
        return None
    layout = body.get("layout", {})
    if not isinstance(layout, dict):
        return None
    return layout


def _walk_tree_leaves(node: Any) -> list[dict[str, Any]]:
    """Yield every leaf node in the inspect tree, depth-first.

    Each leaf is the raw ``{"type": "leaf", "panels": [...], ...}`` dict
    so callers can read its ``panels`` (with ``ref`` / ``active`` flags).
    """
    if not isinstance(node, dict):
        return []
    if node.get("type") == "leaf":
        return [node]
    if node.get("type") == "branch":
        leaves: list[dict[str, Any]] = []
        for child in node.get("children", []) or []:
            leaves.extend(_walk_tree_leaves(child))
        return leaves
    return []


def _find_leaf_for_ref(layout: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """Return the leaf node containing ``ref``'s panel, or None if not present."""
    tree = layout.get("tree")
    for leaf in _walk_tree_leaves(tree):
        for panel in leaf.get("panels", []) or []:
            if panel.get("ref") == ref:
                return leaf
    return None


def _find_panel_summary(layout: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """Return the flat panel summary for ``ref``, or None if not present."""
    for panel in layout.get("panels", []) or []:
        if panel.get("ref") == ref:
            return panel
    return None


def _require_open(op: str, *refs: str) -> int | None:
    """Pre-flight check: every named ref must already be a live panel.

    Without this, ops on a closed / nonexistent panel post-and-wait the
    full ``_wait_stable`` cap before reporting failure, since the
    frontend's ``handleX`` silently no-ops on null lookups (no error path
    back to the broadcaster). The script has the layout in hand already,
    so we can short-circuit. ``self`` resolves server-side to the
    requester's chat and can't be cheaply checked here -- it's treated
    as always present. ``https://`` URLs are accepted as refs in some
    contexts (notably ``open``) where the panel doesn't yet exist; those
    callers don't pass them to this check. Returns ``EXIT_ERROR`` after
    writing a stderr line if any ref is missing, else ``None``. A
    transient ``inspect`` failure (returns ``None``) is treated as "can't
    tell, proceed" so we don't block legitimate work on a flaky read.

    Honors ``MINDS_LAYOUT_NO_WAIT_STABLE``: that escape hatch is for
    environments with no live frontend, where ``inspect`` returns nothing
    meaningful and this pre-flight would spuriously block every op. Its
    documented contract is "return as soon as the POST succeeds", so the
    inspect-based check is skipped along with the wait-stable path.
    """
    if os.environ.get(ENV_NO_WAIT_STABLE):
        return None
    layout = _fetch_layout()
    if layout is None:
        return None
    missing: list[str] = []
    for ref in refs:
        if ref == _SELF_REF:
            continue
        if _find_panel_summary(layout, ref) is None:
            missing.append(ref)
    if not missing:
        return None
    listed = ", ".join(repr(r) for r in missing)
    sys.stderr.write(f"error: {op}: ref {listed} is not open in the current layout\n")
    return EXIT_ERROR


def _refs_in_group(leaf: dict[str, Any]) -> list[str]:
    """Tab-mate refs in order, with the active tab marked by a trailing ``*``."""
    out: list[str] = []
    for panel in leaf.get("panels", []) or []:
        ref = panel.get("ref")
        if not isinstance(ref, str):
            continue
        out.append(f"{ref}*" if panel.get("active") else ref)
    return out


def _describe_group(leaf: dict[str, Any] | None) -> str:
    """One-line "tabs=[r1,r2*,r3]" describing a leaf, or ``<absent>`` for None."""
    if leaf is None:
        return "<absent>"
    refs = _refs_in_group(leaf)
    return "tabs=[" + ", ".join(refs) + "]"


# ---------- Per-op predicates and diff descriptions ----------


_Predicate = Callable[[dict[str, Any]], bool]
_NoopMessage = Callable[[dict[str, Any]], str]
_DiffMessage = Callable[[dict[str, Any], dict[str, Any]], str]


def _predicate_ref_present(ref: str) -> _Predicate:
    return lambda layout: _find_panel_summary(layout, ref) is not None


def _predicate_ref_absent(ref: str) -> _Predicate:
    return lambda layout: _find_panel_summary(layout, ref) is None


def _predicate_focus(ref: str) -> _Predicate:
    def check(layout: dict[str, Any]) -> bool:
        leaf = _find_leaf_for_ref(layout, ref)
        if leaf is None:
            return False
        for panel in leaf.get("panels", []) or []:
            if panel.get("ref") == ref:
                return bool(panel.get("active"))
        return False

    return check


def _predicate_title(ref: str, title: str) -> _Predicate:
    def check(layout: dict[str, Any]) -> bool:
        panel = _find_panel_summary(layout, ref)
        return panel is not None and panel.get("title") == title

    return check


def _predicate_url(ref: str, url: str) -> _Predicate:
    def check(layout: dict[str, Any]) -> bool:
        panel = _find_panel_summary(layout, ref)
        return panel is not None and panel.get("url") == url

    return check


def _find_iframe_panel_by_url(
    layout: dict[str, Any], url: str
) -> dict[str, Any] | None:
    """Return the first iframe panel whose ``url`` matches, or None.

    Used by ``open`` / ``split`` against an external ``https://`` target:
    the resulting panel's ref is ``url:<short_hash>``, not the literal
    URL, so ``_predicate_ref_present`` can't match. Scan by ``url``
    instead, restricted to ``panel_type == "iframe"`` so we don't
    accidentally match an unrelated chat / subagent panel.
    """
    for panel in layout.get("panels", []) or []:
        if panel.get("panel_type") == "iframe" and panel.get("url") == url:
            return panel
    return None


def _predicate_url_panel_present(url: str) -> _Predicate:
    """True when any iframe panel in the layout has ``url`` equal to ``url``.

    Companion to ``_find_iframe_panel_by_url`` -- used as the wait-stable
    predicate for ``open`` / ``split`` with an ``https://`` target, since
    the frontend dedups on URL and the server-emitted ref is a
    panel-id-derived ``url:<hash>`` rather than the literal URL.
    """
    return lambda layout: _find_iframe_panel_by_url(layout, url) is not None


def _leaf_for_url(layout: dict[str, Any], url: str) -> dict[str, Any] | None:
    """Locate the leaf containing the URL panel, for use in diff messages.

    Resolves the URL to its ``url:<hash>`` ref via the flat panels list,
    then defers to ``_find_leaf_for_ref``. Returns None if either lookup
    misses.
    """
    panel = _find_iframe_panel_by_url(layout, url)
    if panel is None:
        return None
    ref = panel.get("ref")
    if not isinstance(ref, str):
        return None
    return _find_leaf_for_ref(layout, ref)


def _predicate_share_group(ref: str, anchor_ref: str) -> _Predicate:
    """True when ``ref`` and ``anchor_ref`` are tab-mates in the same group.

    Used by ``move --direction=within``: the post-op invariant is "the
    panel ended up in the anchor's group". Both refs must be present and
    in the same leaf node.
    """

    def check(layout: dict[str, Any]) -> bool:
        ref_leaf = _find_leaf_for_ref(layout, ref)
        anchor_leaf = _find_leaf_for_ref(layout, anchor_ref)
        if ref_leaf is None or anchor_leaf is None:
            return False
        return ref_leaf is anchor_leaf

    return check


def _predicate_any_change(before: dict[str, Any]) -> _Predicate:
    """Relaxed predicate: the layout differs from the snapshot taken before the op.

    Used for cardinal-direction ``move`` where the exact end position
    depends on whether a sibling group exists -- we know the panel moved
    when *something* in the layout changes.
    """
    before_blob = json.dumps(before, sort_keys=True)

    def check(layout: dict[str, Any]) -> bool:
        return json.dumps(layout, sort_keys=True) != before_blob

    return check


# Marker predicate: signals "no observable layout-state change to confirm".
# Used by ``maximize`` / ``restore`` / ``refresh`` -- the broadcast lands
# but layout.json doesn't reflect anything. ``_wait_stable`` short-circuits
# when it sees this and prints the "broadcast sent" stderr note.
_UNOBSERVABLE: _Predicate = lambda _layout: True  # noqa: E731


# ---------- Wait-stable runner ----------


def _wait_stable(
    op: str,
    predicate: _Predicate,
    *,
    cap: float = _WAIT_STABLE_CAP_SECONDS,
    poll: float = _WAIT_STABLE_POLL_SECONDS,
) -> tuple[str, dict[str, Any] | None]:
    """Poll ``inspect`` until ``predicate(layout)`` holds or ``cap`` elapses.

    Returns ``(status, layout)`` where status is one of ``"changed"`` (the
    predicate held within the cap), ``"timeout"`` (the cap elapsed without
    the predicate holding), or ``"unknown"`` (inspect returned no parseable
    layout -- treat as a soft error). The returned layout is the
    last-observed state in all cases (None on inspect failure).
    """
    deadline = time.monotonic() + cap
    last: dict[str, Any] | None = None
    while True:
        layout = _fetch_layout()
        if layout is None:
            sys.stderr.write(
                f"warning: inspect failed while waiting for {op!r} to settle\n"
            )
            return "unknown", last
        last = layout
        if predicate(layout):
            return "changed", layout
        if time.monotonic() >= deadline:
            return "timeout", layout
        time.sleep(poll)


def _run_mutating_op(
    op: str,
    args: dict[str, Any],
    predicate: _Predicate,
    *,
    on_success: _DiffMessage,
    on_noop: _NoopMessage,
    skip_pre_op_noop: bool = False,
) -> int:
    """Standard wrapper for a mutating op: snapshot, post, wait, diff.

    Steps: (1) snapshot the current layout; (2) if the predicate already
    holds, emit a no-op message on stderr and return 0; (3) POST the op;
    (4) on HTTP success, capture the allocated ref (terminal) on stdout;
    (5) wait for the predicate to hold or time out; (6) emit a one-line
    diff or a timeout error on stderr.

    For ops with no observable layout change, pass ``_UNOBSERVABLE`` as
    the predicate -- the wrapper short-circuits the snapshot / wait /
    diff path and just confirms the broadcast went out.

    The ``MINDS_LAYOUT_NO_WAIT_STABLE`` env var bypasses the snapshot /
    wait / diff path entirely; used by the broadcast-pipeline test that
    has no live frontend to apply the op.

    ``skip_pre_op_noop`` disables step (2) for callers whose predicate is
    snapshot-relative -- ie. captures a "before" baseline and reports
    True whenever the layout has moved off it (see
    ``_predicate_any_change``). Such a predicate is meaningless for
    pre-op no-op detection: a fresh ``_fetch_layout()`` here would race
    against autosave / other ops and either spuriously match (skipping
    the POST) or trivially miss; either way the answer is not "the op
    is already a no-op". Callers using invariant predicates (eg.
    "ref is present", "title equals X") leave this False.
    """
    if predicate is _UNOBSERVABLE:
        status, body = _post_layout(op, args)
        if status != 200:
            return _report_failure(op, status, body)
        _emit_allocated_ref(body)
        sys.stderr.write(
            "(broadcast sent; no observable layout-state change to confirm)\n"
        )
        return EXIT_OK

    if os.environ.get(ENV_NO_WAIT_STABLE):
        status, body = _post_layout(op, args)
        if status != 200:
            return _report_failure(op, status, body)
        _emit_allocated_ref(body)
        return EXIT_OK

    before = _fetch_layout()
    if not skip_pre_op_noop and before is not None and predicate(before):
        sys.stderr.write(on_noop(before))
        return EXIT_OK

    status, body = _post_layout(op, args)
    if status != 200:
        return _report_failure(op, status, body)
    _emit_allocated_ref(body)

    wait_status, after = _wait_stable(op, predicate)
    if wait_status == "changed" and after is not None:
        # ``before`` may be None if the pre-op inspect failed transiently
        # but the post-op poll recovered. The success diff still makes
        # sense -- we have the ``after`` half; pass an empty dict in
        # place of the missing ``before`` so the message callbacks (which
        # mostly read fields off ``after``) still produce output.
        sys.stderr.write(on_success(before or {}, after))
        return EXIT_OK
    if wait_status == "timeout":
        sys.stderr.write(
            f"error: timeout waiting for {op!r} to settle after {_WAIT_STABLE_CAP_SECONDS:.0f}s\n"
        )
        return EXIT_ERROR
    sys.stderr.write("(broadcast sent; could not read inspect to confirm new state)\n")
    return EXIT_OK


def _run_terminal_creation_op(op: str, args: dict[str, Any]) -> int:
    """``open`` / ``split`` against ``service:terminal``: always allocates a fresh ref.

    The server pre-mints the ``terminal:<hash>`` ref and returns it in
    the HTTP response, so the snapshot-predicate-then-post pattern that
    other ops use doesn't apply: there's no pre-known ref to predicate
    against. We POST first, then wait for the server-returned ref to
    appear in inspect.
    """
    if os.environ.get(ENV_NO_WAIT_STABLE):
        status, body = _post_layout(op, args)
        if status != 200:
            return _report_failure(op, status, body)
        _emit_allocated_ref(body)
        return EXIT_OK

    status, body = _post_layout(op, args)
    if status != 200:
        return _report_failure(op, status, body)
    allocated = body.get("ref") if isinstance(body, dict) else None
    _emit_allocated_ref(body)
    if not isinstance(allocated, str):
        sys.stderr.write("(broadcast sent; server did not return an allocated ref)\n")
        return EXIT_OK
    wait_status, after = _wait_stable(op, _predicate_ref_present(allocated))
    if wait_status == "changed" and after is not None:
        leaf = _find_leaf_for_ref(after, allocated)
        sys.stderr.write(f"created {allocated} in {_describe_group(leaf)}\n")
        return EXIT_OK
    if wait_status == "timeout":
        sys.stderr.write(
            f"error: timeout waiting for {op!r} to settle after {_WAIT_STABLE_CAP_SECONDS:.0f}s\n"
        )
        return EXIT_ERROR
    sys.stderr.write("(broadcast sent; could not read inspect to confirm new state)\n")
    return EXIT_OK


# ---------- Compact rendering for inspect / where ----------


def _format_tree_compact(node: Any, indent: int = 0) -> list[str]:
    """Render the inspect tree as one line per group, indented by depth.

    Format:
      ``row size=1.0``             (branch headers; ``column`` for vertical stack)
      ``  [chat:alice* terminal:abc] size=0.4``  (leaf groups; ``*`` marks active tab)

    Returns a list of formatted lines so callers can append/prepend
    without dealing with embedded newlines.
    """
    pad = "  " * indent
    if not isinstance(node, dict):
        return []
    if node.get("type") == "leaf":
        refs = _refs_in_group(node)
        size = node.get("size_ratio")
        size_str = f" size={size}" if size is not None else ""
        return [f"{pad}[{' '.join(refs)}]{size_str}"]
    if node.get("type") == "branch":
        arrangement = node.get("arrangement", "?")
        size = node.get("size_ratio")
        size_str = f" size={size}" if size is not None else ""
        out = [f"{pad}{arrangement}{size_str}"]
        for child in node.get("children", []) or []:
            out.extend(_format_tree_compact(child, indent + 1))
        return out
    return []


def _emit_layout_view(layout: dict[str, Any], *, as_json: bool, verbose: bool) -> None:
    """Print the inspect / where layout block in the requested form.

    - ``--json``: structured object, full detail (machine-readable).
    - ``--verbose`` (text): existing YAML tree dump (full detail).
    - default (text): compact one-line-per-group rendering, plus an
      ``active_panel:`` header when set. ``panel_id`` / URL detail is
      suppressed here; pass ``--verbose`` to see them.
    """
    if as_json:
        sys.stdout.write(json.dumps(layout, indent=2))
        sys.stdout.write("\n")
        return
    if verbose:
        yaml.safe_dump(layout, sys.stdout, sort_keys=False, default_flow_style=False)
        return
    active = layout.get("active_panel")
    if active is not None:
        sys.stdout.write(f"active_panel: {active}\n")
    tree = layout.get("tree")
    if tree is None:
        sys.stdout.write("(no layout)\n")
        return
    for line in _format_tree_compact(tree):
        sys.stdout.write(line + "\n")


# ---------- where: neighbor lookup by tree structure ----------


def _build_leaf_parents(
    node: Any, parent_chain: tuple[dict[str, Any], ...]
) -> dict[int, tuple[dict[str, Any], ...]]:
    """Map ``id(leaf)`` -> the chain of ancestor branches (root first).

    Used by ``_neighbors_in_direction`` to walk *up* from a leaf to the
    nearest ancestor of the matching arrangement, then *over* to the
    child subtree that holds the requested neighbors.
    """
    out: dict[int, tuple[dict[str, Any], ...]] = {}
    if not isinstance(node, dict):
        return out
    if node.get("type") == "leaf":
        out[id(node)] = parent_chain
        return out
    if node.get("type") == "branch":
        extended = (*parent_chain, node)
        for child in node.get("children", []) or []:
            out.update(_build_leaf_parents(child, extended))
    return out


def _neighbors_in_direction(
    layout: dict[str, Any], leaf: dict[str, Any], direction: str
) -> list[dict[str, Any]]:
    """Leaves adjacent to ``leaf`` in ``direction``, found by walking the tree.

    Adjacency is structural rather than pixel-precise: we find the
    nearest ancestor branch whose ``arrangement`` matches the requested
    axis (``row`` for left/right, ``column`` for above/below), then take
    the child subtree on the appropriate side of ``leaf`` and collect
    its leaves. Returns the empty list when no neighbor exists in that
    direction.
    """
    tree = layout.get("tree")
    if tree is None:
        return []
    parents_by_leaf = _build_leaf_parents(tree, ())
    chain = parents_by_leaf.get(id(leaf), ())
    if not chain:
        return []
    target_arrangement = "row" if direction in ("left", "right") else "column"
    side = "before" if direction in ("left", "above") else "after"

    # Walk from the innermost ancestor outward; at each branch, if it's
    # the right arrangement, find which child subtree contains our leaf
    # and collect leaves from the neighbor subtree on the requested side.
    current: dict[str, Any] = leaf
    for ancestor in reversed(chain):
        if ancestor.get("arrangement") != target_arrangement:
            current = ancestor
            continue
        children = ancestor.get("children", []) or []
        try:
            idx = next(i for i, c in enumerate(children) if c is current)
        except StopIteration:
            return []
        if side == "before" and idx > 0:
            return _walk_tree_leaves(children[idx - 1])
        if side == "after" and idx < len(children) - 1:
            return _walk_tree_leaves(children[idx + 1])
        current = ancestor
    return []


# ---------- Subcommand handlers ----------


def _cmd_list(args: argparse.Namespace) -> int:
    status, body = _post_layout("list", {})
    if status != 200 or not isinstance(body, dict):
        return _report_failure("list", status, body)
    entries = body.get("entries", [])
    _emit_structured(entries, args.json)
    return EXIT_OK


def _cmd_inspect(args: argparse.Namespace) -> int:
    status, body = _post_layout("inspect", {})
    if status != 200 or not isinstance(body, dict):
        return _report_failure("inspect", status, body)
    layout = body.get("layout", {})
    if not isinstance(layout, dict):
        layout = {}
    _emit_layout_view(layout, as_json=args.json, verbose=args.verbose)
    return EXIT_OK


def _cmd_where(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    # ``self`` is a sentinel the *server* resolves to the caller's chat
    # panel (using the agent_id header). Client-side we don't know the
    # caller's chat ref (``chat:<name>``), so we can't do a tree lookup
    # for ``self`` here -- reject it with a pointer to the explicit form
    # rather than silently returning a "not currently open" error. Done
    # before the ``_fetch_layout()`` round-trip so an unreachable server
    # still surfaces the actionable "use the explicit ref" message
    # instead of a misleading "inspect failed" one.
    if ref == _SELF_REF:
        sys.stderr.write(
            "error: 'self' is not directly resolvable from the CLI; pass the explicit "
            "chat ref (e.g. ``chat:<your-name>``) or use ``inspect`` to see all refs\n"
        )
        return EXIT_ERROR
    layout = _fetch_layout()
    if layout is None:
        sys.stderr.write("error: inspect failed; could not locate the panel\n")
        return EXIT_ERROR
    leaf = _find_leaf_for_ref(layout, ref)
    if leaf is None:
        sys.stderr.write(f"error: ref {ref!r} is not currently open\n")
        return EXIT_ERROR

    panel_summary = _find_panel_summary(layout, ref) or {}
    view: dict[str, Any] = {
        "ref": ref,
        "title": panel_summary.get("title"),
        "panel_type": panel_summary.get("panel_type"),
        "group": {
            "size_ratio": leaf.get("size_ratio"),
            "tabs": _refs_in_group(leaf),
        },
        "neighbors": {
            direction: [
                r
                for n in _neighbors_in_direction(layout, leaf, direction)
                for r in _refs_in_group(n)
            ]
            for direction in _CARDINAL_DIRECTIONS
        },
    }
    if args.verbose:
        view["full_layout"] = layout
    if args.json:
        sys.stdout.write(json.dumps(view, indent=2))
        sys.stdout.write("\n")
        return EXIT_OK
    if args.verbose:
        yaml.safe_dump(view, sys.stdout, sort_keys=False, default_flow_style=False)
        return EXIT_OK
    # Compact text rendering: one line per direction, plus the group line.
    sys.stdout.write(f"ref:    {ref}\n")
    if panel_summary.get("title"):
        sys.stdout.write(f"title:  {panel_summary['title']}\n")
    sys.stdout.write(f"group:  [{' '.join(view['group']['tabs'])}]\n")
    for direction in _CARDINAL_DIRECTIONS:
        neighbor_refs = view["neighbors"][direction]
        rendered = "[" + " ".join(neighbor_refs) + "]" if neighbor_refs else "-"
        sys.stdout.write(f"{direction:<7} {rendered}\n")
    return EXIT_OK


def _cmd_open(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.target)
    # Validate before the registration wait so empty-name / malformed refs
    # fail instantly instead of spending the full registration timeout
    # polling for an obviously-bogus service name.
    _validate_ref(ref)
    if ref.startswith("service:"):
        service_name = ref.removeprefix("service:")
        if not _wait_for_registration(service_name, _REGISTRATION_TIMEOUT_SECONDS):
            sys.stderr.write(
                f"error: service {service_name!r} is not registered in {_applications_file()} "
                f"after waiting {_REGISTRATION_TIMEOUT_SECONDS:.0f}s. "
                f"Did you forward_port.py / start the service?\n"
            )
            return EXIT_ERROR
    payload: dict[str, Any] = {"ref": ref, "new_group": bool(args.new_group)}

    # ``service:terminal`` always creates a fresh tab (no dedup), so the
    # post-op predicate is "the server-allocated terminal ref is now
    # present". The allocated ref comes back in the HTTP response body,
    # so we have to POST first, then wait. For every other ``open``
    # target the ref is stable: if it's already present the op is a
    # no-op (skips the POST entirely; ``focus`` is the explicit
    # bring-to-foreground op), otherwise we wait for the new panel to
    # appear.
    if ref == "service:terminal":
        return _run_terminal_creation_op("open", payload)

    # ``https://`` URL targets create ad-hoc URL panels whose
    # server-emitted ref is ``url:<short_hash(panel_id)>`` -- the literal
    # URL never appears as a ref. Predicate by ``url`` field instead so
    # wait-stable / no-op detection work for external-URL opens.
    if ref.startswith("https://"):
        return _run_mutating_op(
            "open",
            payload,
            _predicate_url_panel_present(ref),
            on_success=lambda b, a: (
                f"opened {ref} (url panel) -- now in "
                f"{_describe_group(_leaf_for_url(a, ref))}\n"
            ),
            on_noop=lambda b: (
                f"no change: {ref} is already open in "
                f"{_describe_group(_leaf_for_url(b, ref))}\n"
            ),
        )

    return _run_mutating_op(
        "open",
        payload,
        _predicate_ref_present(ref),
        on_success=lambda b, a: (
            f"opened {ref} in {_describe_group(_find_leaf_for_ref(a, ref))}\n"
        ),
        on_noop=lambda b: (
            f"no change: {ref} is already open in {_describe_group(_find_leaf_for_ref(b, ref))}\n"
        ),
    )


def _cmd_focus(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    if (err := _require_open("focus", ref)) is not None:
        return err
    return _run_mutating_op(
        "focus",
        {"ref": ref},
        _predicate_focus(ref),
        on_success=lambda b, a: f"focused {ref}\n",
        on_noop=lambda b: f"no change: {ref} is already the active tab in its group\n",
    )


def _cmd_split(args: argparse.Namespace) -> int:
    # Reject incompatible flag combinations before any network / polling work
    # so users get immediate feedback rather than waiting through
    # ``_wait_for_registration`` only to be told the flags can't be used
    # together.
    if args.direction == _WITHIN_DIRECTION and args.new_group:
        sys.stderr.write(
            f"error: --new-group is meaningless with --direction={_WITHIN_DIRECTION} "
            f"(within tabs into the anchor's own group; a new group would defeat the point)\n"
        )
        return EXIT_ERROR
    ref = _normalize_ref(args.target)
    # Validate before the registration wait (see ``_cmd_open`` for the
    # rationale).
    _validate_ref(ref)
    if ref.startswith("service:"):
        service_name = ref.removeprefix("service:")
        if not _wait_for_registration(service_name, _REGISTRATION_TIMEOUT_SECONDS):
            sys.stderr.write(
                f"error: service {service_name!r} is not registered in {_applications_file()} "
                f"after waiting {_REGISTRATION_TIMEOUT_SECONDS:.0f}s.\n"
            )
            return EXIT_ERROR
    relative_to = _normalize_ref(args.relative_to)
    _validate_ref(relative_to)
    # ``split`` creates ``ref`` but anchors against ``relative_to``; only the
    # anchor must already be a live panel. (``ref`` may legitimately be a
    # closed agent / not-yet-rendered service the script is about to
    # surface; the frontend handles creation.)
    if (err := _require_open("split", relative_to)) is not None:
        return err
    payload: dict[str, Any] = {
        "ref": ref,
        "relative_to": relative_to,
        "direction": args.direction,
        "ratio": args.ratio,
        "new_group": bool(args.new_group),
    }

    # ``service:terminal`` always allocates a fresh ref; same pattern as
    # ``open``. For other refs the predicate is "ref is now present" --
    # if it's already open, ``split`` reports a no-op without posting
    # (the frontend's focus-existing side effect is skipped along with
    # the broadcast; use ``focus`` if you want to switch tabs).
    if ref == "service:terminal":
        return _run_terminal_creation_op("split", payload)

    # ``https://`` URL targets are predicate-by-URL for the same reason
    # ``open`` is: the server emits ``url:<hash>``, not the literal URL.
    if ref.startswith("https://"):
        return _run_mutating_op(
            "split",
            payload,
            _predicate_url_panel_present(ref),
            on_success=lambda b, a: (
                f"split: {ref} (url panel) now in "
                f"{_describe_group(_leaf_for_url(a, ref))}\n"
            ),
            on_noop=lambda b: (
                f"no change: {ref} is already open in "
                f"{_describe_group(_leaf_for_url(b, ref))}\n"
            ),
        )

    return _run_mutating_op(
        "split",
        payload,
        _predicate_ref_present(ref),
        on_success=lambda b, a: (
            f"split: {ref} now in {_describe_group(_find_leaf_for_ref(a, ref))}\n"
        ),
        on_noop=lambda b: (
            f"no change: {ref} is already open in {_describe_group(_find_leaf_for_ref(b, ref))}\n"
        ),
    )


def _cmd_close(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    return _run_mutating_op(
        "close",
        {"ref": ref},
        _predicate_ref_absent(ref),
        on_success=lambda b, a: f"closed {ref}\n",
        on_noop=lambda b: f"no change: {ref} is already closed\n",
    )


def _cmd_move(args: argparse.Namespace) -> int:
    # Cheap flag-compatibility check up front so users get immediate
    # feedback on misuse, mirroring ``_cmd_split``.
    if args.direction == _WITHIN_DIRECTION and args.new_group:
        sys.stderr.write(
            f"error: --new-group is meaningless with --direction={_WITHIN_DIRECTION} "
            f"(within targets the anchor's own group)\n"
        )
        return EXIT_ERROR
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    relative_to = _normalize_ref(args.relative_to)
    _validate_ref(relative_to)
    if (err := _require_open("move", ref, relative_to)) is not None:
        return err
    payload: dict[str, Any] = {
        "ref": ref,
        "relative_to": relative_to,
        "direction": args.direction,
        "new_group": bool(args.new_group),
    }

    # For ``within`` with an explicit anchor ref the predicate is precise
    # (ref + anchor share a leaf) and behaves as a real invariant -- pre-op
    # no-op detection works correctly. ``within`` with
    # ``--relative-to=self`` falls back to ``any_change`` because the
    # ``self`` sentinel never appears as a real ref in inspect output --
    # the frontend resolves it server-side, but client-side we have no way
    # to map it to the caller's ``chat:<name>`` leaf without an extra
    # round trip. For cardinal directions the exact end-position depends
    # on whether a sibling group exists in that direction (frontend
    # resolves dynamically); ``any_change`` is the right relaxed
    # predicate, built against a snapshot taken right before the post.
    # ``any_change`` is snapshot-relative, not invariant-based, so we
    # pass ``skip_pre_op_noop=True`` -- testing it against a
    # ``_run_mutating_op``-fetched ``before`` snapshot can spuriously
    # match if any state changes in the gap, dropping the POST. When
    # wait-stable is bypassed (test mode), skip the snapshot --
    # ``_run_mutating_op`` will short-circuit before the predicate is
    # ever called.
    skip_pre_op_noop = False
    if args.direction == _WITHIN_DIRECTION and relative_to != _SELF_REF:
        predicate: _Predicate = _predicate_share_group(ref, relative_to)
        on_noop: _NoopMessage = lambda b: (
            f"no change: {ref} is already in the same group as {relative_to}\n"
        )
    elif os.environ.get(ENV_NO_WAIT_STABLE):
        # Predicate is unused; pick anything that won't fire the
        # ``_UNOBSERVABLE`` short-circuit (which prints the "no
        # observable change" note that doesn't apply to cardinal move).
        predicate = lambda _layout: False  # noqa: E731
        on_noop = lambda b: ""  # noqa: E731
    else:
        before_snapshot = _fetch_layout()
        if before_snapshot is None:
            sys.stderr.write(
                "warning: inspect failed before move; will not detect a no-op\n"
            )
            before_snapshot = {}
        predicate = _predicate_any_change(before_snapshot)
        # Snapshot-relative predicate -- not usable for pre-op no-op detection.
        skip_pre_op_noop = True
        on_noop = lambda b: ""  # noqa: E731

    return _run_mutating_op(
        "move",
        payload,
        predicate,
        on_success=lambda b, a: (
            f"moved {ref} into {_describe_group(_find_leaf_for_ref(a, ref))}\n"
        ),
        on_noop=on_noop,
        skip_pre_op_noop=skip_pre_op_noop,
    )


def _cmd_rename(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    if (err := _require_open("rename", ref)) is not None:
        return err
    title = args.title
    return _run_mutating_op(
        "rename",
        {"ref": ref, "title": title},
        _predicate_title(ref, title),
        on_success=lambda b, a: (
            f"renamed {ref}: {(_find_panel_summary(b, ref) or {}).get('title')!r} -> {title!r}\n"
        ),
        on_noop=lambda b: f"no change: {ref} is already titled {title!r}\n",
    )


def _cmd_maximize(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    if (err := _require_open("maximize", ref)) is not None:
        return err
    return _run_mutating_op(
        "maximize",
        {"ref": ref},
        _UNOBSERVABLE,
        on_success=lambda b, a: "",
        on_noop=lambda b: "",
    )


def _cmd_restore(_args: argparse.Namespace) -> int:
    return _run_mutating_op(
        "restore",
        {},
        _UNOBSERVABLE,
        on_success=lambda b, a: "",
        on_noop=lambda b: "",
    )


def _cmd_replace_url(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    if (err := _require_open("replace-url", ref)) is not None:
        return err
    _validate_replace_url(args.url)
    # The frontend rewrites ``service:<name>[/<path>]`` to ``/service/<name>...``
    # before storing it on the panel; the wait-stable predicate must compare
    # against that resolved form, not the literal shorthand the agent typed.
    expected_url = _resolve_replace_url(args.url)
    return _run_mutating_op(
        "replace-url",
        {"ref": ref, "url": args.url},
        _predicate_url(ref, expected_url),
        on_success=lambda b, a: (
            f"replace-url {ref}: {(_find_panel_summary(b, ref) or {}).get('url')!r} -> {expected_url!r}\n"
        ),
        on_noop=lambda b: f"no change: {ref} is already pointed at {expected_url!r}\n",
    )


def _cmd_refresh(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.target)
    _validate_ref(ref)
    # ``refresh service:<name>`` reloads *every* iframe for the service
    # (multi-iframe broadcast on the server side), so the named ref need
    # not itself be open -- skip the precheck for that form. Every other
    # target reloads a single specific panel and only makes sense if that
    # panel is currently open.
    if not ref.startswith("service:"):
        if (err := _require_open("refresh", ref)) is not None:
            return err
    return _run_mutating_op(
        "refresh",
        {"ref": ref},
        _UNOBSERVABLE,
        on_success=lambda b, a: "",
        on_noop=lambda b: "",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_list = subparsers.add_parser("list", help="List addressable services + agents")
    p_list.add_argument("--json", action="store_true", help="Emit JSON instead of YAML")
    p_list.set_defaults(func=_cmd_list)

    p_inspect = subparsers.add_parser(
        "inspect", help="Describe the live dockview state (compact text by default)"
    )
    p_inspect.add_argument(
        "--json", action="store_true", help="Emit JSON (full detail)"
    )
    p_inspect.add_argument(
        "--verbose",
        action="store_true",
        help="Emit the full YAML tree (panel_id, URL, etc.) instead of the compact view",
    )
    p_inspect.set_defaults(func=_cmd_inspect)

    p_where = subparsers.add_parser(
        "where",
        help="Show one panel's group tab-mates and the refs in each cardinal direction",
    )
    p_where.add_argument(
        "ref", help="Panel ref to locate (service name shorthand accepted)"
    )
    p_where.add_argument(
        "--json", action="store_true", help="Emit JSON instead of text"
    )
    p_where.add_argument(
        "--verbose",
        action="store_true",
        help="Also include the full inspect layout under ``full_layout``",
    )
    p_where.set_defaults(func=_cmd_where)

    p_open = subparsers.add_parser("open", help="Surface a service in the UI")
    p_open.add_argument(
        "target",
        help="Service name, ref, or https:// URL (e.g. ``web``, ``service:web``, ``https://example.com``)",
    )
    p_open.add_argument(
        "--new-group",
        action="store_true",
        help=(
            "Force a brand-new dockview group instead of tabbing into an existing "
            "right-side group (the default reuses adjacent groups when present)."
        ),
    )
    p_open.set_defaults(func=_cmd_open)

    p_focus = subparsers.add_parser(
        "focus", help="Activate the named panel within its group"
    )
    p_focus.add_argument("ref", help="Panel ref")
    p_focus.set_defaults(func=_cmd_focus)

    p_split = subparsers.add_parser("split", help="Open a new panel as a split")
    p_split.add_argument(
        "target",
        help="Service name, ref, or https:// URL to open as the new panel",
    )
    p_split.add_argument(
        "--relative-to",
        default="self",
        help="Ref to split relative to. ``self`` (default) resolves to the caller's chat panel.",
    )
    p_split.add_argument(
        "--direction",
        default="right",
        choices=_DIRECTIONS,
        help=(
            "Where to place the new panel relative to the anchor. ``left`` / "
            "``right`` / ``above`` / ``below`` target the *adjacent* group in "
            "that direction. ``within`` tabs the panel into the anchor's "
            "*own* group (the single-call form of 'put X in the same group "
            "as Y'). Default: ``right``."
        ),
    )
    p_split.add_argument(
        "--ratio",
        type=float,
        default=0.6,
        help=(
            "Fraction the new panel occupies (0..1). Ignored when "
            "--direction=within, because the panel tabs into the anchor's "
            "own group and size hints don't apply."
        ),
    )
    p_split.add_argument(
        "--new-group",
        action="store_true",
        help=(
            "Force a brand-new dockview group instead of tabbing into the group "
            "that already lives in the requested direction (the default). "
            "Rejected when combined with --direction=within."
        ),
    )
    p_split.set_defaults(func=_cmd_split)

    p_close = subparsers.add_parser("close", help="Remove a panel")
    p_close.add_argument("ref", help="Panel ref")
    p_close.set_defaults(func=_cmd_close)

    p_move = subparsers.add_parser(
        "move", help="Relocate an existing panel (state-preserving)"
    )
    p_move.add_argument("ref", help="Panel ref to move")
    p_move.add_argument("--relative-to", required=True, help="Ref to move relative to")
    p_move.add_argument(
        "--direction",
        required=True,
        choices=_DIRECTIONS,
        help=(
            "Where to land the moved panel. Cardinal directions (``left`` / "
            "``right`` / ``above`` / ``below``) target the adjacent group on "
            "that side. ``within`` tabs the panel into the anchor's own group."
        ),
    )
    p_move.add_argument(
        "--new-group",
        action="store_true",
        help=(
            "Force a brand-new dockview group instead of moving the panel into "
            "an adjacent existing group (the default). Rejected when combined "
            "with --direction=within."
        ),
    )
    p_move.set_defaults(func=_cmd_move)

    p_rename = subparsers.add_parser("rename", help="Update a panel's tab title")
    p_rename.add_argument("ref", help="Panel ref")
    p_rename.add_argument("title", help="New tab title")
    p_rename.set_defaults(func=_cmd_rename)

    p_max = subparsers.add_parser("maximize", help="Maximize a panel's group")
    p_max.add_argument("ref", help="Panel ref")
    p_max.set_defaults(func=_cmd_maximize)

    p_restore = subparsers.add_parser("restore", help="Exit a maximized group")
    p_restore.set_defaults(func=_cmd_restore)

    p_replace = subparsers.add_parser("replace-url", help="Swap an iframe's src")
    p_replace.add_argument("ref", help="Panel ref")
    p_replace.add_argument(
        "url",
        help="``service:<name>[/<path>]`` shorthand or a full https:// URL",
    )
    p_replace.set_defaults(func=_cmd_replace_url)

    p_refresh = subparsers.add_parser(
        "refresh", help="Reload an iframe (or all iframes for a service)"
    )
    p_refresh.add_argument(
        "target",
        help="Panel ref. ``service:<name>`` reloads every iframe for that service.",
    )
    p_refresh.set_defaults(func=_cmd_refresh)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
