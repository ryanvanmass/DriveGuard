<p align="center">
  <img src="assets/logo-wordmark.svg" alt="DriveGuard" width="480">
</p>

**Copy a drive. Verify every byte. Prove it to your client.**

DriveGuard is a Python wrapper around `rsync` built for professional drive-to-drive
migrations. It doesn't just copy your files — it checksums every one of them after
the transfer, automatically re-copies anything that doesn't match, and hands you a
clean HTML report you can actually send to a client.

No more "I think it copied fine." DriveGuard checks.

## Why

Plain `rsync` is fast and reliable, but it doesn't give you:
- Proof that every file arrived intact (not just that it was "sent")
- Automatic recovery when a file gets corrupted in transit
- Something presentable to hand off after the job

DriveGuard adds all three on top of rsync, without reinventing the wheel underneath.

## How it works

```
┌─────────────────┐     ┌──────────────────────┐     ┌───────────────────┐
│  1. Transfer     │ ──▶ │ 2. Checksum verify    │ ──▶ │ 3. Final verify    │
│  rsync -aHAX     │     │  + auto-retransfer    │     │  (confirm it fixed │
│  (live progress) │     │  rsync --checksum     │     │   everything)      │
└─────────────────┘     └──────────────────────┘     └───────────────────┘
                                                                │
                                                                ▼
                                                    ┌────────────────────┐
                                                    │  HTML report        │
                                                    │  original → new     │
                                                    │  path, status, notes│
                                                    └────────────────────┘
```

1. **Initial transfer** — a full `rsync -aHAX` pass preserving permissions,
   timestamps, ownership, hard links, ACLs, and extended attributes. Shows
   rsync's live progress bar (% done, transfer rate, ETA) right in your terminal.
2. **Checksum verification + auto-retransfer** — re-reads every file on both
   sides and compares actual content checksums, not just size/timestamp.
   Anything that doesn't match (or is missing) gets automatically re-copied.
3. **Final verification pass** — a dry-run checksum check to confirm the
   retransfer actually fixed things. Anything still broken after a retry is
   flagged for manual attention instead of silently reported as fine.

The result is an HTML report with summary cards, a **Failed Transfers** table up top (so problems jump out immediately), and a full **All Files** table underneath with everything:

| Original Path | New Path | Status | Notes |
|---|---|---|---|
| `/old/drive/photos/img001.jpg` | `/new/drive/backup/photos/img001.jpg` | Verified | |
| `/old/drive/docs/report.docx` | `/new/drive/backup/docs/report.docx` | Verified (auto-retransferred) | Checksum mismatch detected after initial transfer; automatically re-copied and re-verified. |
| `/old/drive/logs/corrupt.log` | `/new/drive/backup/logs/corrupt.log` | Failed | Permission denied (13) |

If nothing failed, the Failed Transfers table is replaced with a green all-clear banner instead of an empty table. Optionally generate a PDF version of the same report with `--pdf` — handy for emailing a client or archiving alongside the job.

### Technician report

Add `--tech-report` to also generate a second, internal-facing report meant for
you or your team rather than the client. It leads with a **directory-level
failure rollup** — every failed file's path collapsed to (by default) 4
directory levels deep, ranked by failure count, showing only the top 2
directories by default so the worst offenders jump out immediately without
scrolling past thousands of individual rows (any remaining directories are
summarized in a one-line note) — followed by the **full error list** with the
complete error message for every failure and which pass it happened in
(initial transfer, checksum retransfer, or final verification), plus any raw
rsync output that didn't cleanly map to a specific file. Files that failed
checksum but were auto-fixed are called out separately too, since a cluster of
those can be an early sign of a flaky cable or a dying source drive even
though nothing technically failed.

```bash
python3 driveguard.py /mnt/old_drive /mnt/new_drive/client_backup --tech-report
```

By default this writes `<report>_technician.html` next to your main report. Use
`--tech-report PATH` for a custom name, `--tech-report-depth N` to change how
many directory levels the rollup collapses to (default 4), and
`--tech-report-top-dirs N` to change how many directories show in the summary
table (default 2). `--pdf` covers both reports if `--tech-report` is set.

## Requirements

- Linux
- Python 3.7+
- `rsync` (3.1+ recommended, for `--info=progress2` support)

The HTML reports themselves need no extra packages. PDF export (`--pdf`) needs one
of the following (DriveGuard tries them in this order and tells you which one
it used):
- `pip install weasyprint --break-system-packages` (pure Python, recommended)
- `wkhtmltopdf` installed system-wide (`sudo apt install wkhtmltopdf`)

If neither is present, `--pdf` prints install instructions and the run still
completes normally — you just get the HTML report(s) without a PDF alongside them.

## Usage

```bash
python3 driveguard.py /path/to/source /path/to/destination
```

That's it — full pipeline, sensible defaults, HTML report written to
`transfer_report.html` in the current directory.

### A more real-world example

```bash
sudo python3 driveguard.py /mnt/old_drive /mnt/new_drive/client_backup \
    --report acme_corp_migration_report.html \
    --title "Acme Corp — Drive Migration, Aug 2026" \
    --tech-report \
    --pdf
```

Run this inside `tmux` or `screen` for large drives — it can take a while, and
you don't want an SSH drop to kill the job.

### Options

| Flag | Default | Description |
|---|---|---|
| `--report PATH` | `transfer_report.html` | Where to write the HTML report |
| `--title TEXT` | `Data Transfer Report` | Report heading (put your client name here) |
| `--rsync-args "..."` | `-aHAX --partial --info=progress2` | rsync flags for the transfer/checksum passes |
| `--pdf` | off | Also render PDF version(s) of the report(s) |
| `--pdf-report PATH` | `<report>.pdf` | Custom output path for the client report's PDF |
| `--failures-only` | off | Skip the "All Files" table entirely and only show the Failed Transfers table (summary counts still cover everything) |
| `--tech-report [PATH]` | off | Also generate the technician report (see above). Optional custom path; default `<report>_technician.html` |
| `--tech-report-depth N` | `4` | Directory levels to roll failures up to in the technician report's summary table |
| `--tech-report-top-dirs N` | `2` | How many directories to show in the technician report's failure summary, ranked by failure count |
| `--no-checksum-verify` | off | Skip checksum verification + auto-retransfer entirely (old plain-copy behavior) |
| `--no-final-verify` | off | Skip the final confirmation pass (still does the auto-retransfer, just doesn't double-check it afterward) |


### Default rsync flags, explained

- `-a` — archive mode: recurse, preserve symlinks/permissions/timestamps/ownership
- `-H` — preserve hard links
- `-A` — preserve ACLs
- `-X` — preserve extended attributes (xattrs)
- `--partial` — keep partially-transferred files so an interrupted run can resume instead of restarting
- `--info=progress2` — live overall progress bar

If your destination filesystem doesn't support ACLs/xattrs (e.g. FAT/exFAT),
drop them: `--rsync-args "-aH --partial --info=progress2"`

## A note on time

Checksum verification reads every byte of every file on **both** sides of the
transfer. For large drives, that roughly doubles the total I/O time compared to
a plain copy. That's the actual cost of a real integrity check, not a bug —
use `--no-checksum-verify` if you'd rather skip it for speed.

## Notes on scale

For very large file counts (500k+), the HTML report table can get heavy in a
browser. Use `--failures-only` to keep the detail table lean — summary counts
still reflect the full job either way.

## License

MIT — do whatever you want with it.
