"""End-to-end tests running the real driveguard.py CLI against a real
rsync -- no mocking of the transfer pipeline itself. Kept to a handful of
small files so this stays fast; test_cli_convert_pdf.py and
test_pdf_conversion.py cover PDF rendering in isolation.
"""
import os
import subprocess
import sys

import pytest

DRIVEGUARD_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "driveguard.py")
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "rsync"], capture_output=True).returncode != 0,
    reason="rsync not installed",
)


def _make_source_tree(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("hello world")
    (src / "sub" / "b.txt").write_text("nested file content")
    (src / "sub" / "c.txt").write_bytes(os.urandom(1024))
    return src


def _run_driveguard(args, cwd=None):
    return subprocess.run(
        [sys.executable, DRIVEGUARD_PATH, *args],
        capture_output=True, text=True, cwd=cwd,
    )


def test_full_transfer_copies_every_file_and_verifies(tmp_path):
    src = _make_source_tree(tmp_path)
    dest = tmp_path / "dest"
    report = tmp_path / "report.html"

    result = _run_driveguard([str(src), str(dest), "--report", str(report)])

    assert result.returncode == 0, result.stdout + result.stderr
    assert (dest / "a.txt").read_text() == "hello world"
    assert (dest / "sub" / "b.txt").read_text() == "nested file content"
    assert (dest / "sub" / "c.txt").read_bytes() == (src / "sub" / "c.txt").read_bytes()
    assert report.is_file()

    out = result.stdout
    assert "3 files found" in out
    assert "All files verified successfully" in out


def test_report_reflects_the_real_transfer(tmp_path):
    src = _make_source_tree(tmp_path)
    dest = tmp_path / "dest"
    report = tmp_path / "report.html"

    _run_driveguard([str(src), str(dest), "--report", str(report)])

    html = report.read_text()
    assert "All Files (3)" in html
    assert '<div class="all-clear">' in html
    assert "a.txt" in html


def test_no_inc_recursive_is_present_in_every_rsync_invocation(tmp_path):
    """Regression test for the progress-bar-percentage-dips bug: rsync's
    incremental recursion (the -a default) grows its progress total
    mid-transfer as more of the tree is discovered, which can make the
    live percentage decrease. --no-inc-recursive forces a full upfront
    scan so the total is fixed from the start."""
    src = _make_source_tree(tmp_path)
    dest = tmp_path / "dest"
    report = tmp_path / "report.html"

    result = _run_driveguard([str(src), str(dest), "--report", str(report)])

    rsync_command_lines = [
        line for line in result.stdout.splitlines() if line.strip().startswith("$ rsync")
    ]
    assert rsync_command_lines, "expected at least one printed rsync command line"
    for line in rsync_command_lines:
        assert "--no-inc-recursive" in line


def test_failed_file_shows_up_in_the_failed_transfers_table(tmp_path):
    src = _make_source_tree(tmp_path)
    dest = tmp_path / "dest"
    report = tmp_path / "report.html"
    # Make the destination read-only for one file's directory so rsync
    # can't write it -- a real, reproducible failure rather than a mock.
    dest.mkdir()
    (dest / "sub").mkdir()
    (dest / "sub" / "b.txt").write_text("placeholder")
    os.chmod(dest / "sub", 0o555)

    try:
        result = _run_driveguard([str(src), str(dest), "--report", str(report)])
    finally:
        os.chmod(dest / "sub", 0o755)  # restore so tmp_path cleanup can remove it

    html = report.read_text()
    assert "Failed Transfers (1)" in html or "Failed" in html
    assert result.returncode == 0  # driveguard itself completes; failures are reported, not fatal


def test_no_checksum_verify_skips_the_checksum_pass(tmp_path):
    src = _make_source_tree(tmp_path)
    dest = tmp_path / "dest"
    report = tmp_path / "report.html"

    result = _run_driveguard(
        [str(src), str(dest), "--report", str(report), "--no-checksum-verify"]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Step 1/1" in result.stdout
    assert "Verifying checksums" not in result.stdout


def test_convert_pdf_after_a_real_transfer_run(tmp_path):
    """The actual point of --convert-pdf: run a real transfer first (no
    --pdf), then convert the resulting report afterward as a separate step."""
    src = _make_source_tree(tmp_path)
    dest = tmp_path / "dest"
    report = tmp_path / "report.html"
    _run_driveguard([str(src), str(dest), "--report", str(report)])
    assert report.is_file()

    site = tmp_path / "fake_site"
    (site / "weasyprint").mkdir(parents=True)
    (site / "weasyprint" / "__init__.py").write_text(
        "class HTML:\n"
        "    def __init__(self, filename): self.filename = filename\n"
        "    def write_pdf(self, pdf_path):\n"
        "        open(pdf_path, 'wb').write(b'%PDF-1.4 fake\\n%%EOF')\n"
    )
    env = dict(os.environ, PYTHONPATH=str(site))
    result = subprocess.run(
        [sys.executable, DRIVEGUARD_PATH, "--convert-pdf", str(report)],
        capture_output=True, text=True, env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "report.pdf").is_file()
