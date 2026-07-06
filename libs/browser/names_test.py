import pytest

from browser import names


@pytest.mark.parametrize(
    "name",
    [
        "alex-smith",
        "a1-b2",
        "alex",
        "a",
        "a-b-c",
        "x9",
    ],
)
def test_is_valid_browser_name_accepts_good_names(name: str) -> None:
    assert names.is_valid_browser_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty
        "Bad",  # uppercase
        "a--b",  # double dash
        "-a",  # leading dash
        "a-",  # trailing dash
        "a/b",  # slash (would break the URL path / profile dir)
        "a b",  # space
        "a_b",  # underscore not allowed
        "alex.smith",  # dot not allowed (no '.'/'..' path components)
        "a" * 41,  # too long (>40)
        "0",  # pure-numeric rejected (so legacy numeric profile dirs don't resurrect)
        "12",  # pure-numeric
    ],
)
def test_is_valid_browser_name_rejects_bad_names(name: str) -> None:
    assert names.is_valid_browser_name(name) is False


def test_generate_browser_name_is_always_valid() -> None:
    # Whatever generator is bound (mngr's ENGLISH or the local fallback), every name it
    # produces must pass server-side validation -- it is used as a URL segment and a
    # profile-dir suffix unchanged.
    for _ in range(200):
        name = names.generate_browser_name()
        assert names.is_valid_browser_name(name), f"generated invalid name: {name!r}"
        assert "-" in name  # ~2-word: a first-last pair joined by a dash


def test_local_fallback_generator_produces_valid_names() -> None:
    # The import-fallback path (when mngr isn't importable) must also produce valid,
    # dash-joined names. Exercise it directly rather than monkeypatching the bound
    # _generate, so the fallback word lists are covered.
    for _ in range(200):
        name = names._local_generate()
        assert names.is_valid_browser_name(name)
        assert name.count("-") == 1  # exactly first-last
