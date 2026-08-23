#!/usr/bin/env python3
"""
driveguard.py

Copies all data from a source path to a destination path using rsync,
verifies every file with a post-transfer checksum pass (auto-retransferring
anything that doesn't match), then generates a clean, client-ready HTML
report showing:
  - Original file path
  - New (destination) file path
  - Status (Verified / Verified-after-retransfer / Failed / Skipped)
  - Notes / error message (if any)

Usage:
    python3 driveguard.py /path/to/source /path/to/destination \
        [--report report.html] [--rsync-args "-aHAX --partial"] [--title "Client Name - Drive Migration"]

Pipeline:
  1. Initial transfer with rsync.
  2. Checksum verification pass (rsync --checksum): re-reads every file on
     both sides and automatically re-copies anything whose content doesn't
     match (or that's missing on the destination).
  3. Final dry-run checksum pass to confirm everything now matches. Files
     that still don't match after an automatic retry are flagged in the
     report as needing manual attention.

Notes:
  - Run from a terminal you can leave running (tmux/screen recommended for
    large drives). rsync itself supports resuming with --partial if it
    gets interrupted; just re-run the same command.
  - The initial transfer and checksum-retransfer passes show rsync's live
    --info=progress2 status bar (current file, % done, transfer rate, ETA)
    right in the console. The final verification pass runs quietly since
    its output has to be captured and parsed rather than streamed -- a
    message is printed before it starts so the console isn't silently
    hanging.
  - Default rsync flags (-aHAX --partial --info=progress2) preserve
    permissions, timestamps, ownership, hard links, ACLs and extended
    attributes -- a solid default for a full drive-to-drive migration.
    Adjust with --rsync-args if needed (e.g. drop -X if the destination
    filesystem doesn't support xattrs).
  - Checksum verification reads every byte of every file on both sides, so
    it roughly doubles the total I/O time on top of the initial copy. That's
    the cost of a real integrity check -- worth it for a client deliverable,
    but use --no-checksum-verify to skip it, or --no-final-verify to skip
    just the confirmation pass (still does the auto-retransfer, just doesn't
    double-check it afterward).
  - For very large file counts (500k+), the HTML table can get heavy in the
    browser. Use --failures-only to only list problem rows in the detailed
    table (summary counts still cover everything).
"""

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

# --- Console styling ------------------------------------------------------
# Colorize only when writing to a real terminal, and respect the NO_COLOR
# convention (https://no-color.org/) for anyone piping output to a file/log.
USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"

def c(text, *codes):
    """Wrap text in ANSI codes, or return it plain if colors are disabled."""
    if not USE_COLOR:
        return text
    return "".join(codes) + text + RESET

def rule():
    width = min(64, shutil.get_terminal_size((80, 20)).columns - 2)
    print(c("-" * width, DIM))

def step_header(text):
    print()
    print(c(f"==> {text}", BOLD, CYAN))

def ok_line(text):
    print(c(f"  [OK] {text}", GREEN))

def warn_line(text):
    print(c(f"  [!]  {text}", YELLOW))

def fail_line(text):
    print(c(f"  [X]  {text}", RED))

def stat_line(label, value, width=32, *codes):
    padded = label.ljust(width)
    print(f"  {c(padded, *codes) if codes else padded}{value}")

def cmd_line(cmd_list):
    print(c(f"  $ {' '.join(cmd_list)}", DIM))


class Heartbeat:
    """Prints a periodic 'still working' line with elapsed time for passes
    that produce no other output (e.g. the final verify pass, which has to
    capture its output for parsing rather than streaming it live)."""
    def __init__(self, message, interval=5):
        self.message = message
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.start_time = None

    def _run(self):
        while not self._stop.wait(self.interval):
            elapsed = time.time() - self.start_time
            print(c(f"  ... {self.message} ({elapsed/60:.1f} min elapsed)", DIM))

    def __enter__(self):
        self.start_time = time.time()
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()

LOGO_SVG = """<svg width="52" height="52" viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="shieldGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1f7ae0"/>
      <stop offset="100%" stop-color="#0b4fa8"/>
    </linearGradient>
  </defs>
  <path d="M100 10 L174 38 L174 106 C174 152 142 182 100 196
           C58 182 26 152 26 106 L26 38 Z"
        fill="url(#shieldGrad)" stroke="#07356f" stroke-width="3"/>
  <circle cx="97" cy="98" r="40" fill="none" stroke="#ffffff" stroke-width="5" opacity="0.95"/>
  <circle cx="97" cy="98" r="9" fill="#ffffff"/>
  <line x1="97" y1="98" x2="134" y2="70" stroke="#ffffff" stroke-width="6" stroke-linecap="round"/>
  <circle cx="134" cy="70" r="6" fill="#ffffff"/>
  <circle cx="150" cy="150" r="28" fill="#1a7f37" stroke="#ffffff" stroke-width="5"/>
  <path d="M137 150 L147 160 L165 138" fill="none" stroke="#ffffff" stroke-width="7"
        stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""

def human_size(n):
    if n is None:
        return "-"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"

def build_manifest(source):
    """Walk the source tree and return {relpath: size_in_bytes}."""
    manifest = {}
    for root, dirs, files in os.walk(source):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, source)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = None
            manifest[rel] = size
    return manifest

def run_rsync(source, dest, rsync_args, log_path, stderr_path):
    src = source if source.endswith(os.sep) else source + os.sep
    cmd = ["rsync"] + rsync_args.split() + [
        f"--log-file={log_path}",
        "--log-file-format=%o|%f|%l",
        src, dest,
    ]
    cmd_line(cmd)
    start = time.time()
    with open(stderr_path, "w") as errf:
        # stdout is intentionally left connected to the terminal (not captured)
        # so rsync's own --info=progress2 status bar renders live on screen.
        proc = subprocess.run(cmd, stderr=errf, text=True)
    elapsed = time.time() - start
    return proc.returncode, elapsed

def run_checksum_retransfer(source, dest, rsync_args, log_path, stderr_path):
    """Real (non-dry-run) pass using --checksum. rsync will re-copy any file
    whose content checksum doesn't match the destination (or that's missing
    entirely). Anything logged here failed the checksum check and was
    automatically retransferred."""
    src = source if source.endswith(os.sep) else source + os.sep
    base_args = rsync_args.split()
    if "--checksum" not in base_args and "-c" not in base_args:
        base_args.append("--checksum")
    cmd = ["rsync"] + base_args + [
        f"--log-file={log_path}",
        "--log-file-format=%o|%f|%l",
        src, dest,
    ]
    cmd_line(cmd)
    start = time.time()
    with open(stderr_path, "w") as errf:
        # Same here -- stdout inherited so the progress bar shows live.
        proc = subprocess.run(cmd, stderr=errf, text=True)
    elapsed = time.time() - start
    return proc.returncode, elapsed

ITEMIZE_RE = re.compile(r'^([<>ch.*][fdLDS][\.\?][\w.\+]*)\|(.*)$')

def run_final_verify(source, dest, rsync_args):
    """Dry-run checksum comparison to confirm nothing still differs after
    the retransfer pass. Returns a set of relpaths that still mismatch.

    Output is captured (not streamed live) since it needs to be parsed for
    itemized changes, so no progress bar shows during this pass -- print a
    heads-up before calling this so the console isn't silent."""
    src = source if source.endswith(os.sep) else source + os.sep
    base_args = [a for a in rsync_args.split() if a not in ("--partial", "--info=progress2")]
    if "--checksum" not in base_args and "-c" not in base_args:
        base_args.append("--checksum")
    cmd = ["rsync"] + base_args + [
        "--dry-run", "--itemize-changes", "--out-format=%i|%n",
        src, dest,
    ]
    cmd_line(cmd)
    start = time.time()
    with Heartbeat("verifying checksums"):
        proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - start

    still_mismatched = set()
    src_trim = source.lstrip(os.sep)
    for line in proc.stdout.splitlines():
        m = ITEMIZE_RE.match(line.strip())
        if not m:
            continue
        itemize_code, fname = m.group(1), m.group(2)
        if len(itemize_code) < 2 or itemize_code[1] != "f":
            continue
        if itemize_code[0] in ".*":
            continue
        if fname.startswith(src_trim + "/"):
            fname = fname[len(src_trim) + 1:]
        elif fname == src_trim:
            fname = ""
        still_mismatched.add(fname)
    return still_mismatched, elapsed

LOG_LINE_RE = re.compile(r'(send|recv)\|(.+)\|(\d+)\s*$')

def parse_log(log_path, source):
    """Return {relpath: size} for lines rsync logged as transferred.

    rsync's --log-file lines are prefixed with a timestamp/pid, and
    depending on version/options %f may come through as a path relative
    to the source root, OR as the full source path with the leading
    slash stripped. Handle both.
    """
    successes = {}
    if not os.path.exists(log_path):
        return successes
    src_trim = source.lstrip(os.sep)
    with open(log_path, errors="replace") as f:
        for line in f:
            m = LOG_LINE_RE.search(line.rstrip("\n"))
            if not m:
                continue
            op, fname, length = m.group(1), m.group(2), m.group(3)
            if fname.startswith(src_trim + "/"):
                fname = fname[len(src_trim) + 1:]
            elif fname == src_trim:
                fname = ""
            try:
                size = int(length)
            except ValueError:
                size = None
            successes[fname] = size
    return successes

ERR_PATTERN = re.compile(r'rsync:\s*(?:\[\w+\]\s*)?[^"]*?"([^"]+)"\s*(?:failed)?[:]?\s*(.*)')

def parse_stderr(stderr_path, source):
    """Return {relpath: error_message}, plus a list of unmatched error lines."""
    errors = {}
    unmatched = []
    if not os.path.exists(stderr_path):
        return errors, unmatched
    src_norm = source if source.endswith(os.sep) else source + os.sep
    with open(stderr_path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("sent ") or line.startswith("total size"):
                continue
            m = ERR_PATTERN.search(line)
            if m:
                path, msg = m.group(1), m.group(2).strip()
                rel = path[len(src_norm):] if path.startswith(src_norm) else path
                errors[rel] = msg if msg else line
            else:
                unmatched.append(line)
    return errors, unmatched

STATUS_CLASS = {
    "Success": "ok",
    "Already up to date": "ok",
    "Verified": "ok",
    "Verified (auto-retransferred)": "fixed",
    "Failed": "fail",
    "Checksum Failed (unresolved)": "fail",
    "Skipped / Unknown": "skip",
}
FAIL_STATUSES = ("Failed", "Checksum Failed (unresolved)")

def compute_rows(source, dest, manifest, successes, errors,
                  retransferred=None, retransfer_errors=None, still_mismatched=None,
                  checksum_ran=False):
    """Single source of truth for per-file status, shared by the client report
    and the technician report so the two can never disagree with each other."""
    retransferred = retransferred or {}
    retransfer_errors = retransfer_errors or {}
    still_mismatched = still_mismatched or set()

    rows = []
    stats = {"total": len(manifest), "ok": 0, "fixed": 0, "fail": 0, "skip": 0}

    for rel, orig_size in sorted(manifest.items()):
        orig_path = os.path.join(source, rel)
        new_path = os.path.join(dest, rel)
        exists_on_dest = os.path.exists(new_path)
        pass_failed = None  # which pass produced the failure, for the technician report

        if rel in still_mismatched:
            status = "Checksum Failed (unresolved)"
            error_msg = ("Content still doesn't match destination after an automatic "
                         "retransfer attempt. Needs manual attention.")
            pass_failed = "Final verification"
            stats["fail"] += 1
        elif rel in retransfer_errors:
            status = "Failed"
            error_msg = retransfer_errors[rel]
            pass_failed = "Checksum retransfer"
            stats["fail"] += 1
        elif rel in retransferred:
            status = "Verified (auto-retransferred)"
            note = "Checksum mismatch detected after initial transfer; automatically re-copied and re-verified."
            if rel in errors:
                note += f" (Initial transfer also reported: {errors[rel]})"
            error_msg = note
            stats["fixed"] += 1
        elif rel in errors and not exists_on_dest:
            status = "Failed"
            error_msg = errors[rel]
            pass_failed = "Initial transfer"
            stats["fail"] += 1
        elif rel in successes:
            status = "Verified" if checksum_ran else "Success"
            error_msg = ""
            stats["ok"] += 1
        elif exists_on_dest:
            if checksum_ran:
                status = "Verified"
                error_msg = "Already present and matched on checksum verification (no changes needed)."
            else:
                status = "Already up to date"
                error_msg = "Skipped by rsync's quick check (size/timestamp already matched); content checksum not verified this run."
            stats["ok"] += 1
        else:
            status = "Skipped / Unknown"
            error_msg = "No matching success or error entry found in rsync output, and file is not present on the destination"
            stats["skip"] += 1

        rows.append({
            "rel": rel, "orig_path": orig_path, "new_path": new_path,
            "status": status, "error_msg": error_msg, "pass_failed": pass_failed,
        })

    return rows, stats

def dir_rollup(rel, max_depth=4):
    """Directory portion of a relative path, collapsed to at most max_depth
    levels so deeply-nested failures still roll up into a readable summary."""
    dirpath = os.path.dirname(rel)
    if not dirpath:
        return "(top-level)"
    parts = dirpath.split(os.sep)
    if len(parts) > max_depth:
        parts = parts[:max_depth] + ["..."]
    return "/".join(parts)

def build_report(source, dest, rows, stats, unmatched,
                  returncode, elapsed, report_path, title, failures_only,
                  checksum_elapsed=None, verify_elapsed=None):
    checksum_ran = checksum_elapsed is not None
    total, ok_count, fixed_count, fail_count, skip_count = (
        stats["total"], stats["ok"], stats["fixed"], stats["fail"], stats["skip"])

    def render_rows(row_list):
        out = []
        for r in row_list:
            cls = STATUS_CLASS[r["status"]]
            out.append(f"""
        <tr class="{cls}">
            <td>{html.escape(r["orig_path"])}</td>
            <td>{html.escape(r["new_path"])}</td>
            <td class="status-cell">{html.escape(r["status"])}</td>
            <td>{html.escape(r["error_msg"])}</td>
        </tr>""")
        return "".join(out)

    failed_rows = [r for r in rows if r["status"] in FAIL_STATUSES]

    if failed_rows:
        failed_table_html = f"""
<h2>Failed Transfers ({len(failed_rows)})</h2>
<table>
<thead><tr><th>Original Path</th><th>New Path</th><th>Status</th><th>Error</th></tr></thead>
<tbody>{render_rows(failed_rows)}</tbody>
</table>"""
    else:
        failed_table_html = """
<div class="all-clear">All files transferred and verified successfully -- no failed transfers.</div>"""

    if failures_only:
        full_table_html = ""
    else:
        full_table_html = f"""
<h2>All Files ({len(rows)})</h2>
<table>
<thead><tr><th>Original Path</th><th>New Path</th><th>Status</th><th>Error</th></tr></thead>
<tbody>{render_rows(rows)}</tbody>
</table>"""

    unmatched_html = ""
    if unmatched:
        items = "".join(f"<li>{html.escape(u)}</li>" for u in unmatched)
        unmatched_html = f"""
        <h3>Other rsync messages</h3>
        <ul class="unmatched">{items}</ul>"""

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    timing_bits = [f"Transfer: {elapsed/60:.1f} min"]
    if checksum_elapsed is not None:
        timing_bits.append(f"Checksum verify: {checksum_elapsed/60:.1f} min")
    if verify_elapsed is not None:
        timing_bits.append(f"Final verify: {verify_elapsed/60:.1f} min")
    timing_str = " &middot; ".join(timing_bits)

    checksum_note = ""
    if checksum_ran:
        checksum_note = (
            "<br>Every file was verified with a full content checksum after transfer; "
            "any mismatches were automatically re-copied and re-checked."
        )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{html.escape(title)}</title>
<style>
    body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; padding: 2rem; background: #f5f6f8; color: #1d1f24; }}
    .brand {{ display: flex; align-items: center; gap: 0.9rem; margin-bottom: 0.25rem; }}
    .brand svg {{ flex-shrink: 0; }}
    .brand-name {{ font-size: 0.8rem; font-weight: 700; letter-spacing: 0.04em; color: #0757a8; text-transform: uppercase; margin-bottom: 0.1rem; }}
    h1 {{ margin: 0; font-size: 1.6rem; }}
    .meta {{ color: #555; margin-bottom: 1.5rem; font-size: 0.9rem; }}
    .summary {{ display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }}
    .card {{ background: #fff; border-radius: 8px; padding: 1rem 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); min-width: 140px; }}
    .card .num {{ font-size: 1.6rem; font-weight: 700; }}
    .card .label {{ color: #666; font-size: 0.85rem; }}
    .card.ok .num {{ color: #1a7f37; }}
    .card.fixed .num {{ color: #0969da; }}
    .card.fail .num {{ color: #cf222e; }}
    .card.skip .num {{ color: #9a6700; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    th, td {{ text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid #e8e8e8; font-size: 0.85rem; word-break: break-all; }}
    th {{ background: #eef0f3; position: sticky; top: 0; }}
    tr.fail {{ background: #fff3f2; }}
    tr.skip {{ background: #fff9e8; }}
    tr.fixed {{ background: #f0f7ff; }}
    .status-cell {{ font-weight: 600; white-space: nowrap; }}
    tr.ok .status-cell {{ color: #1a7f37; }}
    tr.fail .status-cell {{ color: #cf222e; }}
    tr.skip .status-cell {{ color: #9a6700; }}
    tr.fixed .status-cell {{ color: #0969da; }}
    .unmatched {{ background: #fff; border-radius: 8px; padding: 1rem 1.5rem; font-size: 0.8rem; }}
    .unmatched li {{ margin-bottom: 0.25rem; font-family: monospace; }}
    h2 {{ font-size: 1.1rem; margin: 2rem 0 0.75rem; }}
    .all-clear {{ background: #dcfce7; color: #14532d; border-radius: 8px; padding: 1rem 1.5rem;
                  font-weight: 600; margin-top: 1.5rem; }}
</style>
</head>
<body>
<div class="brand">
    {LOGO_SVG}
    <div>
        <div class="brand-name">DriveGuard</div>
        <h1>{html.escape(title)}</h1>
    </div>
</div>
<div class="meta">
    Source: {html.escape(source)}<br>
    Destination: {html.escape(dest)}<br>
    Generated: {generated} &middot; {timing_str} &middot; rsync exit code: {returncode}{checksum_note}
</div>
<div class="summary">
    <div class="card"><div class="num">{total}</div><div class="label">Total files</div></div>
    <div class="card ok"><div class="num">{ok_count}</div><div class="label">{"Verified" if checksum_ran else "Successful"}</div></div>
    {f'<div class="card fixed"><div class="num">{fixed_count}</div><div class="label">Auto-retransferred &amp; verified</div></div>' if checksum_ran else ""}
    <div class="card fail"><div class="num">{fail_count}</div><div class="label">Failed</div></div>
    <div class="card skip"><div class="num">{skip_count}</div><div class="label">Skipped / Unknown</div></div>
</div>
{failed_table_html}
{full_table_html}
{unmatched_html}
</body>
</html>
"""
    with open(report_path, "w") as f:
        f.write(html_doc)

    return {
        "total": total, "ok": ok_count, "fixed": fixed_count,
        "fail": fail_count, "skip": skip_count, "failed_rows": len(failed_rows),
    }

def build_tech_report(source, dest, rows, stats, unmatched, returncode, elapsed,
                       report_path, title, checksum_elapsed=None, verify_elapsed=None,
                       dir_depth=4):
    """Technician-facing report: a directory-level failure rollup up front
    (so problem folders jump out without scrolling past thousands of rows),
    followed by the complete error list with full messages and which pass
    each failure occurred in, plus any raw rsync output that couldn't be
    matched to a specific file."""
    checksum_ran = checksum_elapsed is not None
    failed_rows = [r for r in rows if r["status"] in FAIL_STATUSES]
    fixed_rows = [r for r in rows if r["status"] == "Verified (auto-retransferred)"]

    # --- Directory rollup summary --------------------------------------
    dir_counts = {}
    for r in failed_rows:
        key = dir_rollup(r["rel"], dir_depth)
        dir_counts[key] = dir_counts.get(key, 0) + 1

    if dir_counts:
        dir_rows_sorted = sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        dir_table_rows = "".join(
            f"""
        <tr>
            <td>{html.escape(d)}</td>
            <td class="status-cell">{n}</td>
        </tr>""" for d, n in dir_rows_sorted
        )
        dir_summary_html = f"""
<h2>Failed Directories (up to {dir_depth} levels deep)</h2>
<table>
<thead><tr><th>Directory</th><th>Failed File Count</th></tr></thead>
<tbody>{dir_table_rows}</tbody>
</table>"""
    else:
        dir_summary_html = """
<div class="all-clear">No failed transfers -- nothing to summarize by directory.</div>"""

    # --- Full error list -------------------------------------------------
    if failed_rows:
        error_table_rows = "".join(f"""
        <tr class="fail">
            <td>{html.escape(r["orig_path"])}</td>
            <td>{html.escape(r["new_path"])}</td>
            <td class="status-cell">{html.escape(r["status"])}</td>
            <td>{html.escape(r["pass_failed"] or "-")}</td>
            <td>{html.escape(r["error_msg"])}</td>
        </tr>""" for r in failed_rows)
        error_table_html = f"""
<h2>Full Error List ({len(failed_rows)})</h2>
<table>
<thead><tr><th>Original Path</th><th>New Path</th><th>Status</th><th>Failed During</th><th>Error</th></tr></thead>
<tbody>{error_table_rows}</tbody>
</table>"""
    else:
        error_table_html = """
<div class="all-clear">No errors to report -- every file transferred and verified cleanly.</div>"""

    # --- Auto-retransferred (informational, not a failure but worth a technician's eye) ---
    fixed_html = ""
    if fixed_rows:
        fixed_table_rows = "".join(f"""
        <tr class="fixed">
            <td>{html.escape(r["orig_path"])}</td>
            <td>{html.escape(r["error_msg"])}</td>
        </tr>""" for r in fixed_rows)
        fixed_html = f"""
<h2>Auto-Retransferred Files ({len(fixed_rows)})</h2>
<p class="note">These failed the post-transfer checksum comparison and were automatically
re-copied and re-verified. Not a failure, but worth noting if a pattern shows up here
(e.g. a flaky cable or a failing source drive).</p>
<table>
<thead><tr><th>Original Path</th><th>Detail</th></tr></thead>
<tbody>{fixed_table_rows}</tbody>
</table>"""

    unmatched_html = ""
    if unmatched:
        items = "".join(f"<li>{html.escape(u)}</li>" for u in unmatched)
        unmatched_html = f"""
<h2>Unmatched rsync Output ({len(unmatched)})</h2>
<p class="note">Raw stderr lines from rsync that didn't map cleanly to a specific file
(summary lines, warnings, directory-level errors, etc.) -- included as-is for reference.</p>
<ul class="unmatched">{items}</ul>"""

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timing_bits = [f"Transfer: {elapsed/60:.1f} min"]
    if checksum_elapsed is not None:
        timing_bits.append(f"Checksum verify: {checksum_elapsed/60:.1f} min")
    if verify_elapsed is not None:
        timing_bits.append(f"Final verify: {verify_elapsed/60:.1f} min")
    timing_str = " &middot; ".join(timing_bits)

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{html.escape(title)}</title>
<style>
    body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; padding: 2rem; background: #f5f6f8; color: #1d1f24; }}
    .brand {{ display: flex; align-items: center; gap: 0.9rem; margin-bottom: 0.25rem; }}
    .brand svg {{ flex-shrink: 0; }}
    .brand-name {{ font-size: 0.8rem; font-weight: 700; letter-spacing: 0.04em; color: #6a7280; text-transform: uppercase; margin-bottom: 0.1rem; }}
    h1 {{ margin: 0; font-size: 1.6rem; }}
    h2 {{ font-size: 1.1rem; margin: 2rem 0 0.75rem; }}
    .note {{ color: #666; font-size: 0.85rem; margin: -0.25rem 0 0.75rem; }}
    .meta {{ color: #555; margin-bottom: 1.5rem; font-size: 0.9rem; }}
    .summary {{ display: flex; gap: 1rem; margin-bottom: 1rem; flex-wrap: wrap; }}
    .card {{ background: #fff; border-radius: 8px; padding: 1rem 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); min-width: 140px; }}
    .card .num {{ font-size: 1.6rem; font-weight: 700; }}
    .card .label {{ color: #666; font-size: 0.85rem; }}
    .card.ok .num {{ color: #1a7f37; }}
    .card.fixed .num {{ color: #0969da; }}
    .card.fail .num {{ color: #cf222e; }}
    .card.skip .num {{ color: #9a6700; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    th, td {{ text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid #e8e8e8; font-size: 0.85rem; word-break: break-all; }}
    th {{ background: #eef0f3; position: sticky; top: 0; }}
    tr.fail {{ background: #fff3f2; }}
    tr.fixed {{ background: #f0f7ff; }}
    .status-cell {{ font-weight: 600; white-space: nowrap; }}
    tr.fail .status-cell {{ color: #cf222e; }}
    tr.fixed .status-cell {{ color: #0969da; }}
    .unmatched {{ background: #fff; border-radius: 8px; padding: 1rem 1.5rem; font-size: 0.8rem; }}
    .unmatched li {{ margin-bottom: 0.25rem; font-family: monospace; }}
    .all-clear {{ background: #dcfce7; color: #14532d; border-radius: 8px; padding: 1rem 1.5rem;
                  font-weight: 600; margin-top: 1rem; }}
</style>
</head>
<body>
<div class="brand">
    {LOGO_SVG}
    <div>
        <div class="brand-name">DriveGuard &middot; Technician Report</div>
        <h1>{html.escape(title)}</h1>
    </div>
</div>
<div class="meta">
    Source: {html.escape(source)}<br>
    Destination: {html.escape(dest)}<br>
    Generated: {generated} &middot; {timing_str} &middot; rsync exit code: {returncode}
</div>
<div class="summary">
    <div class="card"><div class="num">{stats['total']}</div><div class="label">Total files</div></div>
    <div class="card ok"><div class="num">{stats['ok']}</div><div class="label">{"Verified" if checksum_ran else "Successful"}</div></div>
    {f'<div class="card fixed"><div class="num">{stats["fixed"]}</div><div class="label">Auto-retransferred</div></div>' if checksum_ran else ""}
    <div class="card fail"><div class="num">{stats['fail']}</div><div class="label">Failed</div></div>
    <div class="card skip"><div class="num">{stats['skip']}</div><div class="label">Skipped / Unknown</div></div>
</div>
{dir_summary_html}
{error_table_html}
{fixed_html}
{unmatched_html}
</body>
</html>
"""
    with open(report_path, "w") as f:
        f.write(html_doc)

def convert_html_to_pdf(html_path, pdf_path):
    """Render the HTML report to PDF. Tries weasyprint first (pure Python,
    `pip install weasyprint --break-system-packages`), then falls back to
    the wkhtmltopdf command-line tool if it's on PATH. Returns (success, tool_used_or_None)."""
    try:
        import weasyprint
        weasyprint.HTML(filename=html_path).write_pdf(pdf_path)
        return True, "weasyprint"
    except ImportError:
        pass
    except Exception as e:
        print(f"weasyprint failed to render the PDF ({e}); trying wkhtmltopdf...")

    wkhtmltopdf_bin = shutil.which("wkhtmltopdf")
    if wkhtmltopdf_bin:
        try:
            subprocess.run([wkhtmltopdf_bin, "--quiet", "--enable-local-file-access",
                            html_path, pdf_path], check=True)
            return True, "wkhtmltopdf"
        except Exception as e:
            print(f"wkhtmltopdf failed to render the PDF: {e}")

    print("\nCouldn't generate a PDF -- no working renderer found.")
    print("Install one of the following and re-run with --pdf:")
    print("  pip install weasyprint --break-system-packages   (pure Python, recommended)")
    print("  sudo apt install wkhtmltopdf                     (system package, alternative)")
    return False, None

def main():
    ap = argparse.ArgumentParser(description="Copy a drive with rsync and produce an HTML transfer report.")
    ap.add_argument("source", help="Source path (e.g. /mnt/old_drive)")
    ap.add_argument("dest", help="Destination path (e.g. /mnt/new_drive/client_backup)")
    ap.add_argument("--report", default="transfer_report.html", help="Output HTML report path")
    ap.add_argument("--rsync-args", default="-aHAX --partial --info=progress2",
                     help="rsync flags to use (default: -aHAX --partial --info=progress2)")
    ap.add_argument("--title", default="Data Transfer Report", help="Report title")
    ap.add_argument("--failures-only", action="store_true",
                     help="Skip the 'All Files' table entirely and only show the Failed Transfers table "
                          "(useful to keep the report light on very large drives)")
    ap.add_argument("--no-checksum-verify", action="store_true",
                     help="Skip post-transfer checksum verification and auto-retransfer (old behavior)")
    ap.add_argument("--no-final-verify", action="store_true",
                     help="Skip the final confirmation pass after auto-retransfer (still does the checksum retransfer itself, just doesn't re-check it)")
    ap.add_argument("--pdf", action="store_true", help="Also generate a PDF version of the report")
    ap.add_argument("--pdf-report", default=None,
                     help="Output path for the PDF (default: same name as --report with a .pdf extension)")
    ap.add_argument("--tech-report", nargs="?", const="__default__", default=None,
                     help="Also generate a technician's report: a directory-level failure rollup "
                          "(up to --tech-report-depth levels deep) plus the full error list with "
                          "every message and which pass it failed in. Optional path; default is "
                          "<report>_technician.html")
    ap.add_argument("--tech-report-depth", type=int, default=4,
                     help="How many directory levels deep to roll failures up to in the technician "
                          "report's directory summary (default: 4)")
    args = ap.parse_args()

    source = os.path.abspath(args.source)
    dest = os.path.abspath(args.dest)

    if not os.path.isdir(source):
        sys.exit(f"Source path does not exist or is not a directory: {source}")
    os.makedirs(dest, exist_ok=True)

    ts = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    log_path = os.path.join("/tmp", f"rsync_log_{ts}.log")
    stderr_path = os.path.join("/tmp", f"rsync_stderr_{ts}.log")

    n_steps = 1 + (0 if args.no_checksum_verify else 1 + (0 if args.no_final_verify else 1))

    print()
    print(c("  DriveGuard", BOLD, BLUE))
    print(c(f"  {source}  ->  {dest}", DIM))
    rule()

    step_header("Scanning source")
    manifest = build_manifest(source)
    total_size = sum(v for v in manifest.values() if v)
    ok_line(f"{len(manifest)} files found ({human_size(total_size)})")

    step_header(f"Step 1/{n_steps} -- Transferring files")
    returncode, elapsed = run_rsync(source, dest, args.rsync_args, log_path, stderr_path)
    if returncode == 0:
        ok_line(f"Transfer finished ({elapsed/60:.1f} min)")
    else:
        warn_line(f"rsync exited with code {returncode} ({elapsed/60:.1f} min) -- see failures below")

    successes = parse_log(log_path, source)
    errors, unmatched = parse_stderr(stderr_path, source)

    retransferred, retransfer_errors, still_mismatched = {}, {}, set()
    checksum_elapsed = verify_elapsed = None

    if not args.no_checksum_verify:
        step_header(f"Step 2/{n_steps} -- Verifying checksums (auto-retransferring mismatches)")
        cs_log_path = os.path.join("/tmp", f"rsync_checksum_log_{ts}.log")
        cs_stderr_path = os.path.join("/tmp", f"rsync_checksum_stderr_{ts}.log")
        cs_returncode, checksum_elapsed = run_checksum_retransfer(
            source, dest, args.rsync_args, cs_log_path, cs_stderr_path)
        retransferred = parse_log(cs_log_path, source)
        retransfer_errors, cs_unmatched = parse_stderr(cs_stderr_path, source)
        unmatched = unmatched + cs_unmatched
        if retransferred:
            warn_line(f"{len(retransferred)} file(s) failed checksum and were automatically retransferred "
                      f"({checksum_elapsed/60:.1f} min)")
        else:
            ok_line(f"All files matched on checksum, nothing to retransfer ({checksum_elapsed/60:.1f} min)")

        if not args.no_final_verify:
            step_header(f"Step 3/{n_steps} -- Final verification")
            still_mismatched, verify_elapsed = run_final_verify(source, dest, args.rsync_args)
            if still_mismatched:
                fail_line(f"{len(still_mismatched)} file(s) still mismatched after retransfer -- "
                          f"needs manual attention ({verify_elapsed/60:.1f} min)")
            else:
                ok_line(f"All files confirmed identical to source ({verify_elapsed/60:.1f} min)")

    step_header("Generating report")
    rows, stats = compute_rows(source, dest, manifest, successes, errors,
                                retransferred=retransferred, retransfer_errors=retransfer_errors,
                                still_mismatched=still_mismatched, checksum_ran=(checksum_elapsed is not None))

    build_report(source, dest, rows, stats, unmatched,
                 returncode, elapsed, args.report, args.title, args.failures_only,
                 checksum_elapsed=checksum_elapsed, verify_elapsed=verify_elapsed)
    ok_line(f"HTML report written to {args.report}")

    pdf_path = None
    if args.pdf:
        pdf_path = args.pdf_report or (os.path.splitext(args.report)[0] + ".pdf")
        pdf_ok, tool = convert_html_to_pdf(args.report, pdf_path)
        if pdf_ok:
            ok_line(f"PDF report written to {pdf_path} (via {tool})")
        else:
            pdf_path = None

    tech_path = tech_pdf_path = None
    if args.tech_report is not None:
        if args.tech_report == "__default__":
            base, ext = os.path.splitext(args.report)
            tech_path = f"{base}_technician{ext or '.html'}"
        else:
            tech_path = args.tech_report
        build_tech_report(source, dest, rows, stats, unmatched, returncode, elapsed,
                           tech_path, f"{args.title} -- Technician Report",
                           checksum_elapsed=checksum_elapsed, verify_elapsed=verify_elapsed,
                           dir_depth=args.tech_report_depth)
        ok_line(f"Technician report written to {tech_path}")

        if args.pdf:
            tech_pdf_path = os.path.splitext(tech_path)[0] + ".pdf"
            pdf_ok, tool = convert_html_to_pdf(tech_path, tech_pdf_path)
            if pdf_ok:
                ok_line(f"Technician PDF written to {tech_pdf_path} (via {tool})")
            else:
                tech_pdf_path = None

    print()
    rule()
    print(c("  Summary", BOLD))
    stat_line("Total files", stats['total'], 32)
    stat_line("Verified", stats['ok'], 32, GREEN)
    if checksum_elapsed is not None:
        stat_line("Auto-retransferred & verified", stats['fixed'], 32, BLUE)
    stat_line("Failed", stats['fail'], 32, RED)
    stat_line("Skipped / Unknown", stats['skip'], 32, YELLOW)
    rule()
    if stats['fail'] > 0:
        fail_line(f"{stats['fail']} file(s) need attention -- see the Failed Transfers table in the report")
    else:
        ok_line("All files verified successfully")
    print(f"  Report: {args.report}" + (f"  |  PDF: {pdf_path}" if pdf_path else ""))
    if tech_path:
        print(f"  Technician report: {tech_path}" + (f"  |  PDF: {tech_pdf_path}" if tech_pdf_path else ""))
    print(c(f"  (raw rsync log: {log_path})", DIM))
    print()

if __name__ == "__main__":
    main()
