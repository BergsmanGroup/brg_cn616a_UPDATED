import argparse
import json
import socket
import uuid


def send_cmd(host: str, port: int, msg: dict, timeout: float = 3.0) -> dict:
    data = (json.dumps(msg) + "\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(data)
        s.settimeout(timeout)
        resp = s.recv(65536).decode("utf-8", errors="ignore").strip()
    return json.loads(resp) if resp else {}


def parse_zones(s: str):
    s = str(s).strip().lower()
    if s == "auto":
        return "auto"
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)

    sub = ap.add_subparsers(dest="op", required=True)

    # basic
    sub.add_parser("ping")
    sub.add_parser("get_status")
    sub.add_parser("shutdown")
    sub.add_parser("restart_serial")
    sub.add_parser("reload_register_map")

    # service config (GUI panel)
    sub.add_parser("get_service_config")
    p = sub.add_parser("set_service_config")
    p.add_argument("--telemetry-hz", type=float, default=None)
    p.add_argument("--config-hz", type=float, default=None)
    p.add_argument("--rampsoak-hz", type=float, default=None)
    p.add_argument("--zones", default=None, help="auto or comma list, e.g. 1,2,3")
    p.add_argument("--flush-each-line", default=None, help="true/false")
    p.add_argument("--analysis-hz", type=float, default=None)
    p.add_argument("--equilibrium-window-s", type=float, default=None)
    p.add_argument("--equilibrium-threshold-c", type=float, default=None)

    # existing ops
    sub.add_parser("read_config")
    sub.add_parser("read_rampsoak")

    p = sub.add_parser("set_sp")
    p.add_argument("--zone", type=int, required=True)
    p.add_argument("--value", type=float, required=True)

    p = sub.add_parser("pid")
    p.add_argument("--zone", type=int, required=True)

    p = sub.add_parser("onoff")
    p.add_argument("--zone", type=int, required=True)

    p = sub.add_parser("set_mode")
    p.add_argument("--zone", type=int, required=True)
    p.add_argument("--mode", required=True, help="e.g. STANDARD")

    p = sub.add_parser("autotune_sp")
    p.add_argument("--zones", required=True, help="zone or comma list, e.g. 2 or 2,3,4")
    p.add_argument("--values", required=True, help="value or comma list, e.g. 100 or 100,100,120")

    p = sub.add_parser("start_autotune")
    p.add_argument("--zones", required=True, help="Comma list, e.g. 2,3,4")

    p = sub.add_parser("stop_autotune")
    p.add_argument("--zone", type=int, required=True)

    args = ap.parse_args()
    cid = uuid.uuid4().hex[:8]

    if args.op == "ping":
        msg = {"id": cid, "op": "ping"}

    elif args.op == "get_status":
        msg = {"id": cid, "op": "get_status"}

    elif args.op == "shutdown":
        msg = {"id": cid, "op": "shutdown"}

    elif args.op == "restart_serial":
        msg = {"id": cid, "op": "restart_serial"}

    elif args.op == "reload_register_map":
        msg = {"id": cid, "op": "reload_register_map"}

    elif args.op == "get_service_config":
        msg = {"id": cid, "op": "get_service_config"}

    elif args.op == "set_service_config":
        patch = {}

        if args.telemetry_hz is not None:
            patch["telemetry_hz"] = float(args.telemetry_hz)
        if args.config_hz is not None:
            patch["config_hz"] = float(args.config_hz)
        if args.rampsoak_hz is not None:
            patch["rampsoak_hz"] = float(args.rampsoak_hz)

        if args.zones is not None:
            z = parse_zones(args.zones)
            if z == "auto":
                patch["zones_mode"] = "auto"
            else:
                patch["zones_mode"] = "list"
                patch["zones_list"] = z

        if args.flush_each_line is not None:
            v = str(args.flush_each_line).strip().lower()
            patch["flush_each_line"] = v in ("1", "true", "yes", "y", "on")

        # NEW: analysis / equilibrium controls
        if args.analysis_hz is not None:
            patch["analysis_hz"] = float(args.analysis_hz)
        if args.equilibrium_window_s is not None:
            patch["equilibrium_window_s"] = float(args.equilibrium_window_s)
        if args.equilibrium_threshold_c is not None:
            patch["equilibrium_threshold_c"] = float(args.equilibrium_threshold_c)

        # build message AFTER patch is complete
        msg = {"id": cid, "op": "set_service_config", "patch": patch}

    elif args.op == "read_config":
        msg = {"id": cid, "op": "read_config"}

    elif args.op == "read_rampsoak":
        msg = {"id": cid, "op": "read_rampsoak"}

    elif args.op == "set_sp":
        msg = {"id": cid, "op": "set_sp_abs", "zone": args.zone, "value_c": args.value}

    elif args.op == "pid":
        msg = {"id": cid, "op": "set_control_method", "zone": args.zone, "method": "PID_CONTROL"}

    elif args.op == "onoff":
        msg = {"id": cid, "op": "set_control_method", "zone": args.zone, "method": "ON_OFF_CONTROL"}

    elif args.op == "set_mode":
        msg = {"id": cid, "op": "set_control_mode", "zone": args.zone, "mode": args.mode}

    elif args.op == "autotune_sp":
        zones = parse_zones(args.zones)
        vals_raw = [x.strip() for x in str(args.values).split(",") if x.strip()]
        vals = [float(x) for x in vals_raw]
        if zones == "auto":
            raise SystemExit("autotune_sp requires explicit zones, not auto")
        if isinstance(zones, list) and len(zones) == 1 and len(vals) == 1:
            msg = {"id": cid, "op": "set_autotune_setpoint", "zone": zones[0], "value_c": vals[0]}
        else:
            msg = {"id": cid, "op": "set_autotune_setpoint", "zones": zones, "setpoints": vals}

    elif args.op == "start_autotune":
        zones = [int(x.strip()) for x in args.zones.split(",") if x.strip()]
        msg = {"id": cid, "op": "start_autotune", "zones": zones}

    elif args.op == "stop_autotune":
        msg = {"id": cid, "op": "stop_autotune", "zone": args.zone}

    else:
        raise SystemExit("Unknown op")

    resp = send_cmd(args.host, args.port, msg)
    print(resp)


if __name__ == "__main__":
    main()