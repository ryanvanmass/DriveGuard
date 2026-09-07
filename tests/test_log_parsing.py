from driveguard import parse_log, parse_stderr


def _write(path, text):
    path.write_text(text)
    return str(path)


def test_parse_log_strips_source_prefix(tmp_path):
    # rsync's %f in the log line never carries a leading slash, even for
    # the "full path" variant -- it's the full path with that slash
    # already stripped (see parse_log's own docstring).
    log_path = _write(
        tmp_path / "rsync.log",
        "2026/09/07 10:00:00 [123] send|mnt/src/photos/img.jpg|2048\n"
        "2026/09/07 10:00:01 [123] send|mnt/src/docs/report.pdf|4096\n",
    )

    successes = parse_log(log_path, "/mnt/src")

    assert successes == {"photos/img.jpg": 2048, "docs/report.pdf": 4096}


def test_parse_log_handles_relative_paths_directly(tmp_path):
    # The other variant %f can take: already relative to the source root.
    log_path = _write(tmp_path / "rsync.log", "... send|photos/img.jpg|2048\n")

    successes = parse_log(log_path, "/mnt/src")

    assert successes == {"photos/img.jpg": 2048}


def test_parse_log_handles_a_file_matching_the_source_root_exactly(tmp_path):
    log_path = _write(tmp_path / "rsync.log", "... send|mnt/src|0\n")

    successes = parse_log(log_path, "/mnt/src")

    assert successes == {"": 0}


def test_parse_log_ignores_unrelated_lines(tmp_path):
    log_path = _write(
        tmp_path / "rsync.log",
        "some unrelated log line\n"
        "... send|mnt/src/a.txt|10\n"
        "another line that doesn't match\n",
    )

    successes = parse_log(log_path, "/mnt/src")

    assert successes == {"a.txt": 10}


def test_parse_log_missing_file_returns_empty_dict():
    assert parse_log("/nonexistent/path.log", "/mnt/src") == {}


def test_parse_stderr_extracts_path_and_message(tmp_path):
    stderr_path = _write(
        tmp_path / "rsync.stderr",
        'rsync: [sender] send_files failed to open "/mnt/src/locked.txt": '
        "Permission denied (13)\n",
    )

    errors, unmatched = parse_stderr(stderr_path, "/mnt/src")

    assert errors == {"locked.txt": "Permission denied (13)"}
    assert unmatched == []


def test_parse_stderr_skips_summary_lines(tmp_path):
    stderr_path = _write(
        tmp_path / "rsync.stderr",
        "sent 1,234 bytes  received 56 bytes  100.00 bytes/sec\n"
        "total size is 9,999  speedup is 1.23\n",
    )

    errors, unmatched = parse_stderr(stderr_path, "/mnt/src")

    assert errors == {}
    assert unmatched == []


def test_parse_stderr_collects_unmatched_lines(tmp_path):
    stderr_path = _write(tmp_path / "rsync.stderr", "some completely unrelated stderr noise\n")

    errors, unmatched = parse_stderr(stderr_path, "/mnt/src")

    assert errors == {}
    assert unmatched == ["some completely unrelated stderr noise"]


def test_parse_stderr_missing_file_returns_empty(tmp_path):
    errors, unmatched = parse_stderr(str(tmp_path / "missing.stderr"), "/mnt/src")
    assert errors == {}
    assert unmatched == []
