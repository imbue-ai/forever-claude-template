from browser import manifest

# The autouse conftest fixture points manifest._MANIFEST_PATH at a fresh per-test tmp.


def test_manifest_roundtrip() -> None:
    written = manifest.Manifest(
        next_id=3,
        browsers=[
            manifest.ManifestEntry(id=0, tabs=["https://www.google.com"], active_tab=0),
            manifest.ManifestEntry(id=2, tabs=["https://a", "https://b"], active_tab=1),
        ],
    )
    manifest.write_manifest(written)
    loaded = manifest.read_manifest()
    assert loaded is not None
    assert loaded.next_id == 3
    assert [e.id for e in loaded.browsers] == [0, 2]
    assert loaded.browsers[1].tabs == ["https://a", "https://b"]
    assert loaded.browsers[1].active_tab == 1
    # The atomic write leaves no temp file behind.
    path = manifest.manifest_path()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_write_fully_replaces_previous() -> None:
    manifest.write_manifest(manifest.Manifest(next_id=1, browsers=[manifest.ManifestEntry(id=0)]))
    manifest.write_manifest(manifest.Manifest(next_id=5, browsers=[]))
    loaded = manifest.read_manifest()
    assert loaded is not None and loaded.next_id == 5 and loaded.browsers == []


def test_read_missing_returns_none() -> None:
    # Nothing written yet in this test's fresh tmp dir.
    assert manifest.read_manifest() is None


def test_read_corrupt_returns_none() -> None:
    path = manifest.manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json")
    assert manifest.read_manifest() is None
    # Valid JSON but wrong schema (extra="forbid") is also treated as missing, not a crash.
    path.write_text('{"version": 1, "totally": "bogus"}')
    assert manifest.read_manifest() is None
