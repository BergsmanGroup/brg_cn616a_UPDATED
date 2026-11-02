---
description: "Qt step 10: finish bounded Logs and diagnostic visibility, validated with repository-derived temporary files."
agent: agent
---
# Step 10 — Logs and diagnostics

Run only step 10; obey the [shared workflow](../../docs/plans/cn616a-qt-gui-workflow.md).
Require step 09 gates. Lower-cost implementation model suitable.

## Read narrowly
Read `docs/reports/qt-gui-step-09.md`, current Qt status/logging, source file names
in [service](../../py/cn616a_service.py), and bounded/rotation patterns in
[history reader](../../py/gui/history_reader.py). Repository diagnostics can be
read locally but must not be copied wholesale into reports.

## Deliver
- `logs.py`: category/source-path selection, timestamps, bounded diagnostic and
  relevant JSONL tails, recording summary, source/read errors and GUI exceptions.
- Limit displayed rows to 5,000; background incremental reads, bounded bytes,
  rotation/truncation/replacement handling and surfaced malformed records.
  No full-history scan per tick; no dependency on the Logs tab being selected
  for error collection or recording status.
- No purge/delete/repair actions on source files. Replay data stays temporary,
  clearly labeled; ordinary diagnostic data never becomes fabricated telemetry.
- Finish basic cross-tab navigation to relevant error/operation status without
  adding rich search, export, themes or new service APIs.

## Automated gate
Test category switches, append, rename rotation, truncation, missing/denied file,
malformed/oversized rows, cap overflow and shutdown while reading. Assert errors
are visible and log scans do not execute on the Qt event thread.

## End gate
Write `docs/reports/qt-gui-step-10.md`. Offline user checks readable error messages,
category/source clarity, recording vs thermal events, long-line behavior and
tab-hidden updates. Stop before step 11.
