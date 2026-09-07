import os
import subprocess
import sys

DRIVEGUARD_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "driveguard.py")
)

_SUCCESS_WEASYPRINT = """
class HTML:
    def __init__(self, filename):
        self.filename = filename
    def write_pdf(self, pdf_path):
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 fake pdf content\\n%%EOF")
"""


def _run(args, env=None):
    return subprocess.run(
        [sys.executable, DRIVEGUARD_PATH, *args],
        capture_output=True, text=True, env=env,
    )


def test_convert_pdf_missing_html_file_errors_clearly(tmp_path):
    result = _run(["--convert-pdf", str(tmp_path / "does_not_exist.html")])

    assert result.returncode == 1
    assert "not found" in (result.stdout + result.stderr)


def test_convert_pdf_combined_with_source_and_dest_is_rejected(tmp_path):
    result = _run([str(tmp_path), str(tmp_path / "dest"), "--convert-pdf", "report.html"])

    assert result.returncode == 2
    assert "--convert-pdf takes no source/dest" in result.stderr


def test_no_arguments_at_all_errors_clearly():
    result = _run([])

    assert result.returncode == 2
    assert "source and dest are required" in result.stderr


def test_convert_pdf_produces_a_real_pdf_at_the_default_path(tmp_path):
    html_path = tmp_path / "report.html"
    html_path.write_text("<html><body>report</body></html>")
    site = tmp_path / "fake_site"
    (site / "weasyprint").mkdir(parents=True)
    (site / "weasyprint" / "__init__.py").write_text(_SUCCESS_WEASYPRINT)

    env = dict(os.environ, PYTHONPATH=str(site))
    result = _run(["--convert-pdf", str(html_path)], env=env)

    expected_pdf = tmp_path / "report.pdf"
    assert result.returncode == 0, result.stdout + result.stderr
    assert expected_pdf.is_file()
    assert expected_pdf.stat().st_size > 0
    assert "via weasyprint" in result.stdout


def test_convert_pdf_respects_custom_pdf_report_path(tmp_path):
    html_path = tmp_path / "report.html"
    html_path.write_text("<html><body>report</body></html>")
    site = tmp_path / "fake_site"
    (site / "weasyprint").mkdir(parents=True)
    (site / "weasyprint" / "__init__.py").write_text(_SUCCESS_WEASYPRINT)
    custom_pdf = tmp_path / "custom_name.pdf"

    env = dict(os.environ, PYTHONPATH=str(site))
    result = _run(["--convert-pdf", str(html_path), "--pdf-report", str(custom_pdf)], env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert custom_pdf.is_file()
    assert not (tmp_path / "report.pdf").exists()
