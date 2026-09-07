from driveguard import build_report, compute_rows


def _build(tmp_path, manifest, successes, errors, failures_only=False, **kwargs):
    dest = tmp_path / "dest"
    dest.mkdir(exist_ok=True)
    for rel in successes:
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    rows, stats = compute_rows(str(tmp_path / "src"), str(dest), manifest, successes, errors, **kwargs)
    report_path = tmp_path / "report.html"
    build_report(
        str(tmp_path / "src"), str(dest), rows, stats, unmatched=[],
        returncode=0, elapsed=1.0, report_path=str(report_path),
        title="Test Report", failures_only=failures_only,
    )
    return report_path.read_text()


def test_all_clear_banner_shown_when_nothing_failed(tmp_path):
    html = _build(tmp_path, {"a.txt": 1}, successes={"a.txt": 1}, errors={})

    assert "no failed transfers" in html
    assert "Failed Transfers (" not in html


def test_failed_transfers_table_shown_when_something_failed(tmp_path):
    html = _build(tmp_path, {"a.txt": 1}, successes={}, errors={"a.txt": "Permission denied"})

    assert "Failed Transfers (1)" in html
    assert '<div class="all-clear">' not in html
    assert "Permission denied" in html


def test_failures_only_omits_the_all_files_table(tmp_path):
    html = _build(
        tmp_path, {"a.txt": 1, "b.txt": 1},
        successes={"a.txt": 1}, errors={"b.txt": "boom"}, failures_only=True,
    )

    assert "All Files (" not in html
    assert "Failed Transfers (1)" in html


def test_all_files_table_present_by_default(tmp_path):
    html = _build(tmp_path, {"a.txt": 1}, successes={"a.txt": 1}, errors={})

    assert "All Files (1)" in html


def test_paths_are_html_escaped(tmp_path):
    manifest = {"<script>alert(1)</script>.txt": 1}
    html = _build(tmp_path, manifest, successes={}, errors={})

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_title_appears_in_the_document(tmp_path):
    html = _build(tmp_path, {"a.txt": 1}, successes={"a.txt": 1}, errors={})

    assert "Test Report" in html


def test_summary_card_counts_match_stats(tmp_path):
    html = _build(
        tmp_path, {"a.txt": 1, "b.txt": 1, "c.txt": 1},
        successes={"a.txt": 1}, errors={"b.txt": "boom"},
    )

    # 3 total, 1 ok, 1 fail, 1 skip (c.txt has no record and isn't on dest)
    assert '<div class="num">3</div>' in html
    assert '<div class="num">1</div>' in html
