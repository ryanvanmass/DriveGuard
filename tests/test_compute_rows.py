import os

from driveguard import compute_rows


def _row_status(rows, rel):
    for r in rows:
        if r["rel"] == rel:
            return r["status"]
    raise AssertionError(f"no row for {rel!r}")


def test_file_in_successes_is_verified_when_checksum_ran(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    manifest = {"a.txt": 5}
    rows, stats = compute_rows(
        str(tmp_path / "src"), str(dest), manifest,
        successes={"a.txt": 5}, errors={}, checksum_ran=True,
    )
    assert _row_status(rows, "a.txt") == "Verified"
    assert stats == {"total": 1, "ok": 1, "fixed": 0, "fail": 0, "skip": 0}


def test_file_in_successes_is_plain_success_without_checksum_pass(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    rows, stats = compute_rows(
        str(tmp_path / "src"), str(dest), {"a.txt": 5},
        successes={"a.txt": 5}, errors={}, checksum_ran=False,
    )
    assert _row_status(rows, "a.txt") == "Success"


def test_file_with_initial_error_and_missing_from_dest_is_failed(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    rows, stats = compute_rows(
        str(tmp_path / "src"), str(dest), {"a.txt": 5},
        successes={}, errors={"a.txt": "Permission denied (13)"},
    )
    row = next(r for r in rows if r["rel"] == "a.txt")
    assert row["status"] == "Failed"
    assert row["pass_failed"] == "Initial transfer"
    assert row["error_msg"] == "Permission denied (13)"
    assert stats["fail"] == 1


def test_file_with_initial_error_but_present_on_dest_is_not_treated_as_failed(tmp_path):
    # rsync can log a transient warning for a file that still made it to
    # the destination -- only "errored AND missing" should count as failed.
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a.txt").write_text("x")
    rows, stats = compute_rows(
        str(tmp_path / "src"), str(dest), {"a.txt": 5},
        successes={}, errors={"a.txt": "some transient warning"},
    )
    assert _row_status(rows, "a.txt") != "Failed"
    assert stats["fail"] == 0


def test_retransferred_file_is_verified_after_retransfer(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    rows, stats = compute_rows(
        str(tmp_path / "src"), str(dest), {"a.txt": 5},
        successes={}, errors={}, retransferred={"a.txt": 5}, checksum_ran=True,
    )
    row = next(r for r in rows if r["rel"] == "a.txt")
    assert row["status"] == "Verified (auto-retransferred)"
    assert stats["fixed"] == 1
    assert stats["fail"] == 0


def test_retransferred_file_folds_in_the_original_transfer_error_as_a_note(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    rows, stats = compute_rows(
        str(tmp_path / "src"), str(dest), {"a.txt": 5},
        successes={}, errors={"a.txt": "checksum mismatch on first pass"},
        retransferred={"a.txt": 5}, checksum_ran=True,
    )
    row = next(r for r in rows if r["rel"] == "a.txt")
    assert "checksum mismatch on first pass" in row["error_msg"]


def test_retransfer_error_takes_priority_and_is_failed(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    rows, stats = compute_rows(
        str(tmp_path / "src"), str(dest), {"a.txt": 5},
        successes={}, errors={}, retransfer_errors={"a.txt": "disk full"},
    )
    row = next(r for r in rows if r["rel"] == "a.txt")
    assert row["status"] == "Failed"
    assert row["pass_failed"] == "Checksum retransfer"
    assert stats["fail"] == 1


def test_still_mismatched_after_final_verify_is_unresolved_failure(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    rows, stats = compute_rows(
        str(tmp_path / "src"), str(dest), {"a.txt": 5},
        successes={"a.txt": 5}, errors={}, still_mismatched={"a.txt"}, checksum_ran=True,
    )
    row = next(r for r in rows if r["rel"] == "a.txt")
    assert row["status"] == "Checksum Failed (unresolved)"
    assert row["pass_failed"] == "Final verification"
    assert stats["fail"] == 1
    # still_mismatched takes priority even over a "successes" entry.
    assert stats["ok"] == 0


def test_file_already_on_dest_without_checksum_pass_is_already_up_to_date(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a.txt").write_text("x")
    rows, stats = compute_rows(
        str(tmp_path / "src"), str(dest), {"a.txt": 5},
        successes={}, errors={}, checksum_ran=False,
    )
    assert _row_status(rows, "a.txt") == "Already up to date"
    assert stats["ok"] == 1


def test_file_already_on_dest_with_checksum_pass_is_verified(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a.txt").write_text("x")
    rows, stats = compute_rows(
        str(tmp_path / "src"), str(dest), {"a.txt": 5},
        successes={}, errors={}, checksum_ran=True,
    )
    assert _row_status(rows, "a.txt") == "Verified"


def test_file_absent_everywhere_with_no_record_is_skipped_unknown(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    rows, stats = compute_rows(
        str(tmp_path / "src"), str(dest), {"a.txt": 5},
        successes={}, errors={},
    )
    assert _row_status(rows, "a.txt") == "Skipped / Unknown"
    assert stats["skip"] == 1


def test_paths_are_joined_correctly(tmp_path):
    rows, _ = compute_rows(
        "/mnt/src", "/mnt/dest", {"sub/a.txt": 5}, successes={}, errors={},
    )
    row = rows[0]
    assert row["orig_path"] == os.path.join("/mnt/src", "sub/a.txt")
    assert row["new_path"] == os.path.join("/mnt/dest", "sub/a.txt")


def test_rows_are_sorted_by_relative_path(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    rows, _ = compute_rows(
        str(tmp_path / "src"), str(dest),
        {"zebra.txt": 1, "alpha.txt": 1, "mid/file.txt": 1},
        successes={}, errors={},
    )
    assert [r["rel"] for r in rows] == ["alpha.txt", "mid/file.txt", "zebra.txt"]
