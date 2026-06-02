import re
from typing import Final
from typing import Self

from imbue.imbue_common.primitives import NonEmptyStr

# A service name is a single URL path component that is interpolated into
# generated JavaScript (the service worker, the bootstrap page) and into a
# cookie name. Restricting it to these characters keeps it safe in all of
# those contexts; anything outside the set could break out of a JS string
# literal or produce a malformed Set-Cookie header.
_SERVICE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9_-]+\Z")


class ServiceName(NonEmptyStr):
    """Name of a service registered under ``runtime/applications.toml`` (e.g. 'web', 'terminal').

    Constrained to ``[A-Za-z0-9_-]`` so the name is safe to interpolate into
    generated client-side code and cookie names. Construction raises
    ``ValueError`` for a name outside that set, so every use site is protected
    by the type rather than by ad-hoc call-site checks.
    """

    def __new__(cls, value: str) -> Self:
        instance = super().__new__(cls, value)
        if _SERVICE_NAME_PATTERN.match(instance) is None:
            raise ValueError(f"{cls.__name__} must match {_SERVICE_NAME_PATTERN.pattern}: {value!r}")
        return instance
