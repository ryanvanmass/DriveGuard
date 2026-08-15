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
import subprocess
import sys
import time
from datetime import datetime

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
    print("Running:", " ".join(cmd))
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
    print("Running checksum verification + auto-retransfer:", " ".join(cmd))
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
    print("Running final verification pass:", " ".join(cmd))
    start = time.time()
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

def build_report(source, dest, manifest, successes, errors, unmatched,
                  returncode, elapsed, report_path, title, failures_only,
                  retransferred=None, retransfer_errors=None, still_mismatched=None,
                  checksum_elapsed=None, verify_elapsed=None):
    retransferred = retransferred or {}
    retransfer_errors = retransfer_errors or {}
    still_mismatched = still_mismatched or set()
    checksum_ran = checksum_elapsed is not None

    total = len(manifest)
    ok_count = 0
    fixed_count = 0
    fail_count = 0
    skip_count = 0
    rows = []

    for rel, orig_size in sorted(manifest.items()):
        orig_path = os.path.join(source, rel)
        new_path = os.path.join(dest, rel)

        if rel in still_mismatched:
            status = "Checksum Failed (unresolved)"
            error_msg = ("Content still doesn't match destination after an automatic "
                         "retransfer attempt. Needs manual attention.")
            fail_count += 1
        elif rel in retransfer_errors:
            status = "Failed"
            error_msg = retransfer_errors[rel]
            fail_count += 1
        elif rel in retransferred:
            status = "Verified (auto-retransferred)"
            note = "Checksum mismatch detected after initial transfer; automatically re-copied and re-verified."
            if rel in errors:
                note += f" (Initial transfer also reported: {errors[rel]})"
            error_msg = note
            fixed_count += 1
        elif rel in errors:
            status = "Failed"
            error_msg = errors[rel]
            fail_count += 1
        elif rel in successes:
            status = "Verified" if checksum_ran else "Success"
            error_msg = ""
            ok_count += 1
        else:
            status = "Skipped / Unknown"
            error_msg = "No matching success or error entry found in rsync output"
            skip_count += 1

        if failures_only and status in ("Success", "Verified", "Verified (auto-retransferred)"):
            continue
        rows.append((orig_path, new_path, status, error_msg))

    status_class = {
        "Success": "ok",
        "Verified": "ok",
        "Verified (auto-retransferred)": "fixed",
        "Failed": "fail",
        "Checksum Failed (unresolved)": "fail",
        "Skipped / Unknown": "skip",
    }

    row_html = []
    for orig_path, new_path, status, error_msg in rows:
        cls = status_class[status]
        row_html.append(f"""
        <tr class="{cls}">
            <td>{html.escape(orig_path)}</td>
            <td>{html.escape(new_path)}</td>
            <td class="status-cell">{html.escape(status)}</td>
            <td>{html.escape(error_msg)}</td>
        </tr>""")

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
    h1 {{ margin-bottom: 0.25rem; }}
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
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
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
<table>
<thead><tr><th>Original Path</th><th>New Path</th><th>Status</th><th>Error</th></tr></thead>
<tbody>{"".join(row_html)}</tbody>
</table>
{unmatched_html}
</body>
</html>
"""
    with open(report_path, "w") as f:
        f.write(html_doc)

def main():
    ap = argparse.ArgumentParser(description="Copy a drive with rsync and produce an HTML transfer report.")
    ap.add_argument("source", help="Source path (e.g. /mnt/old_drive)")
    ap.add_argument("dest", help="Destination path (e.g. /mnt/new_drive/client_backup)")
    ap.add_argument("--report", default="transfer_report.html", help="Output HTML report path")
    ap.add_argument("--rsync-args", default="-aHAX --partial --info=progress2",
                     help="rsync flags to use (default: -aHAX --partial --info=progress2)")
    ap.add_argument("--title", default="Data Transfer Report", help="Report title")
    ap.add_argument("--failures-only", action="store_true", help="Only list non-success rows in the detail table")
    ap.add_argument("--no-checksum-verify", action="store_true",
                     help="Skip post-transfer checksum verification and auto-retransfer (old behavior)")
    ap.add_argument("--no-final-verify", action="store_true",
                     help="Skip the final confirmation pass after auto-retransfer (still does the checksum retransfer itself, just doesn't re-check it)")
    args = ap.parse_args()

    source = os.path.abspath(args.source)
    dest = os.path.abspath(args.dest)

    if not os.path.isdir(source):
        sys.exit(f"Source path does not exist or is not a directory: {source}")
    os.makedirs(dest, exist_ok=True)

    ts = int(time.time())
    log_path = os.path.join("/tmp", f"rsync_log_{ts}.log")
    stderr_path = os.path.join("/tmp", f"rsync_stderr_{ts}.log")

    print("Building file manifest from source...")
    manifest = build_manifest(source)
    print(f"Found {len(manifest)} files.")

    returncode, elapsed = run_rsync(source, dest, args.rsync_args, log_path, stderr_path)

    successes = parse_log(log_path, source)
    errors, unmatched = parse_stderr(stderr_path, source)

    retransferred, retransfer_errors, still_mismatched = {}, {}, set()
    checksum_elapsed = verify_elapsed = None

    if not args.no_checksum_verify:
        cs_log_path = os.path.join("/tmp", f"rsync_checksum_log_{ts}.log")
        cs_stderr_path = os.path.join("/tmp", f"rsync_checksum_stderr_{ts}.log")
        cs_returncode, checksum_elapsed = run_checksum_retransfer(
            source, dest, args.rsync_args, cs_log_path, cs_stderr_path)
        retransferred = parse_log(cs_log_path, source)
        retransfer_errors, cs_unmatched = parse_stderr(cs_stderr_path, source)
        unmatched = unmatched + cs_unmatched

        if not args.no_final_verify:
            print("\nRunning final verification pass (reading all files to confirm checksums match -- "
                  "this pass has no progress bar since output is captured for parsing, please wait)...")
            still_mismatched, verify_elapsed = run_final_verify(source, dest, args.rsync_args)

    build_report(source, dest, manifest, successes, errors, unmatched,
                 returncode, elapsed, args.report, args.title, args.failures_only,
                 retransferred=retransferred, retransfer_errors=retransfer_errors,
                 still_mismatched=still_mismatched, checksum_elapsed=checksum_elapsed,
                 verify_elapsed=verify_elapsed)

    print(f"\nDone. rsync exit code: {returncode}")
    if not args.no_checksum_verify:
        print(f"Checksum-mismatched files auto-retransferred: {len(retransferred)}")
        if still_mismatched:
            print(f"WARNING: {len(still_mismatched)} file(s) still failed verification after retransfer.")
    print(f"Report written to: {args.report}")
    print(f"(raw rsync log: {log_path}, raw stderr: {stderr_path})")

if __name__ == "__main__":
    main()
