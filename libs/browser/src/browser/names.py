"""Random ~2-word english browser names + server-side name validation.

Browsers are addressed by NAME, not a sequential int -- like mngr agent names
(e.g. ``alex-smith``). The name is the addressing key everywhere: the CLI
``<name>`` arg, ``service:browser?session=<name>``, the cast WS path
``/browsers/<name>/cast``, the manifest ``id``, and the persistent profile dir
``browser-use-user-data-dir-<name>``. :func:`is_valid_browser_name` therefore
guarantees a name is safe as a URL path segment, a query value, and a filesystem
path component.

The generator reuses mngr's own agent-name generator
(``imbue.mngr.utils.name_generator.generate_agent_name`` with
``AgentNameStyle.ENGLISH`` -- dash-joined first-last, e.g. ``alex-smith``) when
it is importable, so a daemon-picked name and a frontend-prefilled name look
alike. The import is attempted ONCE at module load (a ``_generate`` callable is
bound), so the importability check costs nothing per call; a small local
first/last word-pair generator is the fallback when mngr is unavailable.
"""

import random
import re

# Lowercase alnum words joined by single dashes, 1..40 chars, no leading/trailing/
# double dash. This keeps a name safe as a URL path segment, a query value, and the
# ``browser-use-user-data-dir-<name>`` profile-dir suffix. Pure-numeric names (e.g.
# "0") are intentionally rejected: see is_valid_browser_name.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_NAME_LEN = 40

# Local fallback word lists (~20 each), lifted verbatim from mngr's
# resources/data/name_lists/agent/english.txt + english_last.txt. Only used when the
# mngr name generator cannot be imported (e.g. a standalone browser-lib install).
_FALLBACK_FIRST = (
    "alex", "blake", "casey", "drew", "elliot", "finn", "harper", "jamie",
    "jordan", "kai", "logan", "morgan", "parker", "quinn", "reese", "riley",
    "sage", "taylor", "tory", "tyler",
)
_FALLBACK_LAST = (
    "smith", "johnson", "williams", "brown", "jones", "davis", "miller",
    "wilson", "moore", "taylor", "anderson", "thomas", "jackson", "white",
    "harris", "martin", "thompson", "garcia", "martinez", "robinson",
)


def _local_generate() -> str:
    """A tiny english first-last name pair (the fallback when mngr isn't importable)."""
    return f"{random.choice(_FALLBACK_FIRST)}-{random.choice(_FALLBACK_LAST)}"


try:
    from imbue.mngr.primitives import AgentNameStyle
    from imbue.mngr.utils.name_generator import generate_agent_name

    def _mngr_generate() -> str:
        # ENGLISH = dash-joined first-last (e.g. "alex-smith"); the SAME source the
        # frontend modal pre-fills from, so a typed name and a generated one look alike.
        return str(generate_agent_name(AgentNameStyle.ENGLISH))

    _generate = _mngr_generate
except ImportError:
    _generate = _local_generate


def generate_browser_name() -> str:
    """Return a random ~2-word english name (e.g. ``alex-smith``).

    Uniqueness within the live fleet is the manager's responsibility (it regenerates
    on collision under its create lock); this just produces a syntactically-valid name.
    """
    return _generate()


def is_valid_browser_name(name: str) -> bool:
    """Whether a user-typed name is a safe browser id.

    Lowercase alnum words joined by single dashes, 1..40 chars, no leading/trailing/
    double dash. This is the server-side validation for a user-supplied name; it
    guarantees the name is safe as a URL path segment, a query value, and the
    ``browser-use-user-data-dir-<name>`` profile-dir suffix (no slashes, no dots, no
    ``.``/``..``). Pure-numeric names are rejected so an upgraded workspace's old
    numeric profile dirs ("0"/"1"/"2") never resurrect as named browsers.
    """
    if not name or len(name) > _MAX_NAME_LEN:
        return False
    if name.isdigit():
        return False
    return NAME_RE.fullmatch(name) is not None
