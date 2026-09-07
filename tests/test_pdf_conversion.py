import os

import pytest

import driveguard
from driveguard import _render_with_weasyprint, convert_html_to_pdf

_CRASH_MODULE = """
import os, signal
class HTML:
    def __init__(self, filename):
        self.filename = filename
    def write_pdf(self, pdf_path):
        os.kill(os.getpid(), signal.SIGSEGV)
"""

_EXCEPTION_MODULE = """
class HTML:
    def __init__(self, filename):
        self.filename = filename
    def write_pdf(self, pdf_path):
        raise ValueError("simulated ordinary rendering error")
"""

_SUCCESS_MODULE = """
class HTML:
    def __init__(self, filename):
        self.filename = filename
    def write_pdf(self, pdf_path):
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 fake pdf content\\n%%EOF")
"""


def _install_fake_weasyprint(tmp_path, module_source, monkeypatch):
    """A fake `weasyprint` package on a scratch site-packages dir, picked up
    by the *subprocess* _render_with_weasyprint spawns (it inherits the
    current environment, PYTHONPATH included, since no `env=` is passed)."""
    site = tmp_path / "fake_site"
    pkg = site / "weasyprint"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(module_source)
    monkeypatch.setenv("PYTHONPATH", str(site))


@pytest.fixture
def html_and_pdf_paths(tmp_path):
    html_path = tmp_path / "report.html"
    html_path.write_text("<html><body>report</body></html>")
    pdf_path = tmp_path / "report.pdf"
    return str(html_path), str(pdf_path)


def test_a_native_crash_is_caught_and_reported_not_silent(tmp_path, monkeypatch, html_and_pdf_paths):
    """The actual bug this was written for: a hard crash (e.g. a real
    Pango/cairo ABI mismatch) must not vanish with zero output."""
    _install_fake_weasyprint(tmp_path, _CRASH_MODULE, monkeypatch)
    html_path, pdf_path = html_and_pdf_paths

    ok, message = _render_with_weasyprint(html_path, pdf_path)

    assert ok is False
    assert message is not None
    assert "crashed" in message
    assert "SIGSEGV" in message
    assert not os.path.exists(pdf_path)


def test_a_plain_exception_surfaces_the_real_traceback(tmp_path, monkeypatch, html_and_pdf_paths):
    _install_fake_weasyprint(tmp_path, _EXCEPTION_MODULE, monkeypatch)
    html_path, pdf_path = html_and_pdf_paths

    ok, message = _render_with_weasyprint(html_path, pdf_path)

    assert ok is False
    assert "simulated ordinary rendering error" in message


def test_a_working_renderer_succeeds_and_produces_a_real_file(tmp_path, monkeypatch, html_and_pdf_paths):
    _install_fake_weasyprint(tmp_path, _SUCCESS_MODULE, monkeypatch)
    html_path, pdf_path = html_and_pdf_paths

    ok, message = _render_with_weasyprint(html_path, pdf_path)

    assert ok is True
    assert message is None
    assert os.path.isfile(pdf_path)
    assert os.path.getsize(pdf_path) > 0


def test_weasyprint_not_installed_is_silent_not_reported(monkeypatch, html_and_pdf_paths):
    # No fake weasyprint installed, and PYTHONPATH deliberately cleared so
    # a real weasyprint on this machine (if any) can't leak into the test.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    html_path, pdf_path = html_and_pdf_paths

    ok, message = _render_with_weasyprint(html_path, pdf_path)

    assert ok is False
    assert message is None  # "not installed" falls through quietly to wkhtmltopdf


def test_convert_html_to_pdf_falls_back_and_reports_no_renderer(monkeypatch, html_and_pdf_paths, capsys):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(driveguard.shutil, "which", lambda name: None)
    html_path, pdf_path = html_and_pdf_paths

    ok, tool = convert_html_to_pdf(html_path, pdf_path)

    assert ok is False
    assert tool is None
    out = capsys.readouterr().out
    assert "Couldn't generate a PDF" in out


def test_convert_html_to_pdf_returns_weasyprint_as_the_tool_on_success(
    tmp_path, monkeypatch, html_and_pdf_paths
):
    _install_fake_weasyprint(tmp_path, _SUCCESS_MODULE, monkeypatch)
    html_path, pdf_path = html_and_pdf_paths

    ok, tool = convert_html_to_pdf(html_path, pdf_path)

    assert ok is True
    assert tool == "weasyprint"


def test_convert_html_to_pdf_falls_back_to_wkhtmltopdf_after_a_weasyprint_crash(
    tmp_path, monkeypatch, html_and_pdf_paths, capsys
):
    # A real (fake) executable on disk -- the wkhtmltopdf branch really
    # shells out via subprocess.run, no need to mock that call itself,
    # just where shutil.which() says the binary lives.
    _install_fake_weasyprint(tmp_path, _CRASH_MODULE, monkeypatch)
    html_path, pdf_path = html_and_pdf_paths

    fake_wkhtmltopdf = tmp_path / "wkhtmltopdf"
    fake_wkhtmltopdf.write_text(f"#!/bin/sh\necho fake wkhtmltopdf output > \"{pdf_path}\"\n")
    fake_wkhtmltopdf.chmod(0o755)
    monkeypatch.setattr(driveguard.shutil, "which", lambda name: str(fake_wkhtmltopdf))

    ok, tool = convert_html_to_pdf(html_path, pdf_path)

    out = capsys.readouterr().out
    assert "weasyprint crashed" in out
    assert ok is True
    assert tool == "wkhtmltopdf"
