from driveguard import build_manifest


def test_manifest_maps_relative_paths_to_sizes(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_bytes(b"world!")

    manifest = build_manifest(str(tmp_path))

    assert manifest == {"a.txt": 5, "sub/b.txt": 6}


def test_empty_source_gives_empty_manifest(tmp_path):
    assert build_manifest(str(tmp_path)) == {}


def test_manifest_handles_a_broken_symlink_without_crashing(tmp_path):
    target = tmp_path / "missing_target"
    link = tmp_path / "broken_link"
    link.symlink_to(target)

    manifest = build_manifest(str(tmp_path))

    # os.path.getsize() raises OSError on a dangling symlink -- build_manifest
    # must record it with an unknown (None) size rather than crashing the scan.
    assert manifest == {"broken_link": None}
