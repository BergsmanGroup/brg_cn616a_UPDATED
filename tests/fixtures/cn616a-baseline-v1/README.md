# Immutable CN616A baseline input v1

This is **synthetic device-level data, not a recording from a physical unit**.
The reference contains complete Modbus RTU FC03 request/response frames as hex,
including unit address, function code, byte count, raw 16-bit register words,
and CRC16. Float words use big-endian IEEE-754 with the most-significant word
first. A frozen register map preserves the physical meaning of these addresses.
Elapsed milliseconds are relative; no machine-specific paths, COM ports, or
service/GUI JSON fields are part of the wire samples.

## Size and reproducibility

- `modbus-reference.json.gz`: **3,823 bytes**, expanding to **33,272 bytes**.
- 16 six-zone telemetry snapshots at 500 ms intervals, plus static system,
  sensor, alarm/scaling/calibration and ramp/soak register responses.
- The 8-second input cycle repeats to generate the requested workload: by
  default 7,200 telemetry rows and 7,200 mock analysis rows over one hour.
  This is a repeatable stress workload, **not an hour-long hardware capture**.
- `manifest.json` records compressed/content SHA-256 hashes and exact sizes.
  Loading verifies both hashes and every transaction's CRC and framing.
- Gzip is lossless. Expanded JSONL files are generated in temporary storage,
  never shipped as a large fixture or read in compressed form by the GUI.

The reference is a permanent input boundary. Do not replace these files in
place when the program changes. Add a new version for a new input corpus and
retain v1 for comparisons. The generator refuses to overwrite an existing
reference. To check reproducibility, generate into a **new temporary directory**
with `tests\baseline_reference.py --output-dir <temporary-directory>` and
compare the manifest hashes.

## How the application uses it

`tests\baseline_reference.py` replays the stored words through an offline
subclass of the current CN616A driver. The subclass never connects and fails
explicitly if the driver requests a register not represented in the reference.
This produces current driver telemetry/config/profile output. The responsiveness
runner then adapts that output to temporary service snapshots and history files
for the current GUI. MAE-style analysis is a mock derived value: the controller
does not transmit analysis or JSON.

Thus **wire input is immutable; application JSON is a replaceable adapter**.
Future real captures can be preserved in the same device-level format with
honest provenance in a new version, without retaining a particular service or
GUI implementation. Copying existing service JSONL via `--copy-logs-from` is
optional workload augmentation, not a substitute for the immutable raw input.

## Run from a clone

On another Windows machine with Python 3.12/Tk, from the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe tests\responsiveness.py verify-data
.\bat\responsiveness_test.bat quick
```

The first check verifies data and reports exact expanded/compressed sizes
without opening a GUI or network listener. The second runs two 30-second
chart-open GUI sessions, with normal and delayed fake service replies.
No controller, production logs, saved service settings, or running service
is needed. The launcher never calls the service-starting GUI wrapper.

Use `responsiveness_test.bat full` for all 24 chart/history/service combinations,
60 seconds each (~25 minutes); no arguments defaults to quick.
Other platforms can invoke `tests\responsiveness.py quick` or `full` with Python 3.12, Tk,
and repository requirements; platform GUI/packaging parity is not certified.
Use `tests\responsiveness.py summarize <report.json>` to print an existing report without a GUI,
network, or third-party dependency.

`tests\responsiveness.py capture --help` documents optional physical raw capture.
It requires an explicit port, a new version directory and confirmation that
no service owns the controller. Never run it alongside the live service.
It calls the capture implementation in `tests\baseline_reference.py`, does not
change controller settings, and preserves v1. Replay a new corpus with
`tests\responsiveness.py quick --reference-dir <new-version-directory>`.
