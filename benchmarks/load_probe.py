#!/usr/bin/env python3
"""
load_probe.py - Multithreaded HTTP load generator and deployment telemetry probe.
Uses ONLY Python standard library.

Parameters:
  --threads: number of concurrent threads (default: 10)
  --duration: test duration in seconds (default: 40.0)
  --pause: pause between requests per thread in seconds (default: 0.01 = 10ms)
  --url: target URL (default: http://127.0.0.1/)
  --host: Host header (default: demo-stream.localhost)
  --deploy-app: Forge app name (optional, e.g. demo-stream)
  --deploy-dir: Forge app directory (optional, e.g. demo-stream)
  --deploy-at: Second from start when deploy should be triggered (default: 15.0)
  --deploy-new-version: Version string to set before deploying (e.g. v2)
  --out: Output JSON file path
"""

import argparse
import http.client
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Forge Blue-Green deployment load probe")
    parser.add_argument("--threads", type=int, default=10, help="Number of worker threads (default: 10)")
    parser.add_argument("--duration", type=float, default=40.0, help="Test duration in seconds (default: 40.0)")
    parser.add_argument("--pause", type=float, default=0.01, help="Pause between requests in seconds (default: 0.01 = 10ms)")
    parser.add_argument("--url", default="http://127.0.0.1/", help="Target URL (default: http://127.0.0.1/)")
    parser.add_argument("--host", default="demo-stream.localhost", help="Host header (default: demo-stream.localhost)")
    parser.add_argument("--timeout", type=float, default=3.0, help="HTTP request timeout in seconds (default: 3.0)")
    parser.add_argument("--deploy-app", default=None, help="Forge app name to trigger deploy on")
    parser.add_argument("--deploy-dir", default=None, help="Directory containing application code")
    parser.add_argument("--deploy-at", type=float, default=15.0, help="Seconds after test start to trigger deploy (default: 15.0)")
    parser.add_argument("--deploy-new-version", default=None, help="Version string to set in server.py before deploying (e.g. v2)")
    parser.add_argument("--forge-api", default="http://127.0.0.1:8000", help="Forge API base URL (default: http://127.0.0.1:8000)")
    parser.add_argument("--db-path", default="forge.db", help="Path to forge.db SQLite database")
    parser.add_argument("--out", default=None, help="Output path for JSON results")
    return parser.parse_args()


def worker(
    thread_id: int,
    stop_event: threading.Event,
    start_time: float,
    parsed_url: urllib.parse.ParseResult,
    host_header: str,
    pause_sec: float,
    timeout_sec: float,
    thread_results: list,
):
    target_host = parsed_url.hostname or "127.0.0.1"
    target_port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
    path = parsed_url.path or "/"
    if parsed_url.query:
        path = f"{path}?{parsed_url.query}"

    conn: http.client.HTTPConnection | None = None

    headers = {
        "Host": host_header,
        "User-Agent": f"ForgeLoadProbe/1.0 (thread {thread_id})",
        "Connection": "keep-alive",
    }

    while not stop_event.is_set():
        req_start = time.perf_counter()
        req_offset = req_start - start_time
        status = None
        error = None
        version = None

        try:
            if conn is None:
                conn = http.client.HTTPConnection(target_host, target_port, timeout=timeout_sec)

            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            status = resp.status

            version = resp.getheader("X-Version")
            body_bytes = resp.read()

            if not version and body_bytes:
                body_str = body_bytes.decode("utf-8", errors="replace")
                m = re.search(r"OK\s+([a-zA-Z0-9_\-\.]+)", body_str)
                if m:
                    version = m.group(1)

            if status != 200:
                error = f"HTTP_{status}"

        except (http.client.RemoteDisconnected, ConnectionResetError) as exc:
            error = "ConnectionReset" if isinstance(exc, ConnectionResetError) else "RemoteDisconnected"
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = None
        except socket.timeout:
            error = "Timeout"
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = None
        except (ConnectionRefusedError, socket.error, OSError) as exc:
            error = f"SocketError_{type(exc).__name__}"
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = None
        except Exception as exc:
            error = f"Exception_{type(exc).__name__}"
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = None

        req_end = time.perf_counter()
        latency_ms = (req_end - req_start) * 1000.0

        thread_results.append((req_offset, status, error, version, latency_ms))

        if pause_sec > 0:
            time.sleep(pause_sec)

    if conn:
        try:
            conn.close()
        except Exception:
            pass


def deploy_trigger_task(
    app_name: str,
    app_dir: str,
    new_version: str,
    deploy_at: float,
    start_time: float,
    forge_api: str,
    deploy_telemetry: dict,
):
    now = time.perf_counter()
    sleep_needed = (start_time + deploy_at) - now
    if sleep_needed > 0:
        time.sleep(sleep_needed)

    deploy_telemetry["requested_at_offset"] = time.perf_counter() - start_time
    print(f"\n[DEPLOY TRIGGER] Triggering deployment of {app_name} with version {new_version} at t={deploy_telemetry['requested_at_offset']:.2f}s...")

    # 1. Update version in server.py
    server_py = Path(app_dir) / "server.py"
    if server_py.is_file():
        code = server_py.read_text(encoding="utf-8")
        code = re.sub(r'APP_VERSION\s*=\s*["\'][^"\']+["\']', f'APP_VERSION = "{new_version}"', code)
        server_py.write_text(code, encoding="utf-8")
        print(f"[DEPLOY TRIGGER] Updated {server_py} APP_VERSION to {new_version}")

    # 2. Trigger deployment via Forge API
    t_submit = time.perf_counter()
    deploy_telemetry["deploy_submit_offset"] = t_submit - start_time

    # First get app_id
    try:
        req = urllib.request.Request(f"{forge_api}/api/v1/applications/{app_name}")
        with urllib.request.urlopen(req, timeout=5) as r:
            app_info = json.loads(r.read().decode())
            app_id = app_info["id"]
    except Exception as exc:
        deploy_telemetry["error"] = f"Failed to get app_id: {exc}"
        print(f"[DEPLOY TRIGGER ERROR] {deploy_telemetry['error']}")
        return

    # Trigger deploy
    try:
        body = json.dumps({"context_path": str(Path(app_dir).resolve())}).encode("utf-8")
        req = urllib.request.Request(
            f"{forge_api}/api/v1/applications/{app_id}/deployments",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read().decode())
            dep_id = res["deployment_id"]
            deploy_telemetry["deployment_id"] = dep_id
            print(f"[DEPLOY TRIGGER] Deployment enqueued: {dep_id}")
    except Exception as exc:
        deploy_telemetry["error"] = f"Failed to enqueue deployment: {exc}"
        print(f"[DEPLOY TRIGGER ERROR] {deploy_telemetry['error']}")
        return

    # 3. Poll deployment status aggressively (every 30ms) to record exact timestamps
    seen_statuses = {}
    dep_active = False
    while not dep_active and (time.perf_counter() - start_time) < 45.0:
        try:
            req = urllib.request.Request(f"{forge_api}/api/v1/deployments/{dep_id}")
            with urllib.request.urlopen(req, timeout=3) as r:
                dep_data = json.loads(r.read().decode())
                curr_status = dep_data.get("status")
                t_poll = time.perf_counter() - start_time
                if curr_status not in seen_statuses:
                    seen_statuses[curr_status] = round(t_poll, 3)
                    print(f"[DEPLOY TRIGGER] Status -> {curr_status} at t={t_poll:.3f}s")
                if curr_status in ("ACTIVE", "FAILED"):
                    deploy_telemetry["final_status"] = curr_status
                    deploy_telemetry["active_container"] = dep_data.get("active_container_id")
                    dep_active = True
                    break
        except Exception:
            pass
        time.sleep(0.03)

    deploy_telemetry["stage_timestamps"] = seen_statuses


def query_db_events(db_path: str, deployment_id: str | None) -> list:
    if not deployment_id or not Path(db_path).is_file():
        return []
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute(
            "SELECT event_kind, created_at, payload FROM events WHERE deployment_id = ? ORDER BY id ASC",
            (deployment_id,),
        )
        rows = cur.fetchall()
        con.close()
        return [{"kind": r[0], "created_at": r[1], "payload": json.loads(r[2]) if r[2] else {}} for r in rows]
    except Exception as exc:
        return [{"query_error": str(exc)}]


def main():
    args = parse_args()
    parsed_url = urllib.parse.urlparse(args.url)

    print("=" * 60)
    print("FORGE BLUE-GREEN DEPLOYMENT LOAD PROBE")
    print(f"Target:       {args.url} (Host: {args.host})")
    print(f"Threads:      {args.threads}")
    print(f"Duration:     {args.duration}s")
    print(f"Pause:        {args.pause * 1000:.1f}ms")
    if args.deploy_new_version:
        print(f"Deploy at:    t={args.deploy_at}s -> new version: {args.deploy_new_version}")
    else:
        print("Deploy:       None (Baseline run)")
    print("=" * 60)

    stop_event = threading.Event()
    start_time = time.perf_counter()

    workers_results = [[] for _ in range(args.threads)]
    worker_threads = []

    for i in range(args.threads):
        t = threading.Thread(
            target=worker,
            args=(
                i,
                stop_event,
                start_time,
                parsed_url,
                args.host,
                args.pause,
                args.timeout,
                workers_results[i],
            ),
            daemon=True,
        )
        worker_threads.append(t)
        t.start()

    deploy_telemetry = {}
    deploy_thread = None
    if args.deploy_new_version and args.deploy_app and args.deploy_dir:
        deploy_thread = threading.Thread(
            target=deploy_trigger_task,
            args=(
                args.deploy_app,
                args.deploy_dir,
                args.deploy_new_version,
                args.deploy_at,
                start_time,
                args.forge_api,
                deploy_telemetry,
            ),
            daemon=True,
        )
        deploy_thread.start()

    time.sleep(args.duration)
    stop_event.set()

    for t in worker_threads:
        t.join(timeout=2.0)

    if deploy_thread:
        deploy_thread.join(timeout=5.0)

    total_time = time.perf_counter() - start_time

    all_records = []
    for r_list in workers_results:
        all_records.extend(r_list)
    all_records.sort(key=lambda x: x[0])

    valid_records = [r for r in all_records if r[0] <= (args.duration + 0.5)]

    total_reqs = len(valid_records)
    ok_reqs = sum(1 for r in valid_records if r[1] == 200)
    non_200_reqs = total_reqs - ok_reqs

    breakdown = Counter()
    for r in valid_records:
        if r[1] == 200:
            breakdown["200 OK"] += 1
        elif r[1] is not None:
            breakdown[f"HTTP {r[1]}"] += 1
        elif r[2] is not None:
            breakdown[r[2]] += 1
        else:
            breakdown["UnknownError"] += 1

    error_seconds = sorted(list(set(int(r[0]) for r in valid_records if r[1] != 200)))

    initial_version = None
    for r in valid_records:
        if r[3]:
            initial_version = r[3]
            break

    first_new_version_offset = None
    first_new_version_sec = None
    new_version_name = None

    if initial_version:
        for r in valid_records:
            if r[3] and r[3] != initial_version:
                first_new_version_offset = r[0]
                first_new_version_sec = int(r[0])
                new_version_name = r[3]
                break

    old_after_new_records = []
    if first_new_version_offset is not None and initial_version:
        old_after_new_records = [
            r for r in valid_records if r[0] > first_new_version_offset and r[3] == initial_version
        ]

    seconds_map = {}
    for r in valid_records:
        sec = int(r[0])
        if sec not in seconds_map:
            seconds_map[sec] = {
                "total": 0,
                "ok": 0,
                "errors": 0,
                "error_details": Counter(),
                "versions": Counter(),
                "latencies": [],
            }
        sm = seconds_map[sec]
        sm["total"] += 1
        if r[1] == 200:
            sm["ok"] += 1
        else:
            sm["errors"] += 1
            err_key = f"HTTP {r[1]}" if r[1] else r[2]
            sm["error_details"][err_key] += 1
        if r[3]:
            sm["versions"][r[3]] += 1
        sm["latencies"].append(r[4])

    db_events = []
    dep_id = deploy_telemetry.get("deployment_id")
    if dep_id:
        db_events = query_db_events(args.db_path, dep_id)

    print("\n" + "=" * 60)
    print("PROBE RESULTS SUMMARY")
    print("=" * 60)
    print(f"Duration:                  {total_time:.2f} s")
    print(f"Total requests:            {total_reqs:,}")
    print(f"Throughput (RPS):          {total_reqs / total_time:.1f} req/s")
    print(f"Successful (200 OK):       {ok_reqs:,} ({ok_reqs / total_reqs * 100:.2f}%)")
    print(f"Non-200 / Errors:          {non_200_reqs:,} ({non_200_reqs / total_reqs * 100:.2f}%)")
    print("\nBreakdown by status / exception:")
    for key, count in breakdown.most_common():
        print(f"  - {key:<25}: {count:,}")

    print(f"\nError seconds:             {error_seconds}")
    print(f"Initial version:           {initial_version}")
    if first_new_version_sec is not None:
        print(f"First new version ({new_version_name}):   at t={first_new_version_offset:.3f}s (second {first_new_version_sec})")
    else:
        print(f"First new version:         None (version did not change during probe)")

    print(f"Old version responses after new version: {len(old_after_new_records)}")
    if old_after_new_records:
        old_after_new_secs = sorted(list(set(int(r[0]) for r in old_after_new_records)))
        print(f"  -> Seconds of old version after new: {old_after_new_secs}")

    if deploy_telemetry:
        print("\nDeployment Telemetry:")
        print(f"  - Deployment ID:         {deploy_telemetry.get('deployment_id')}")
        print(f"  - Requested at:          t={deploy_telemetry.get('requested_at_offset', 0):.2f}s")
        print(f"  - Enqueued at:           t={deploy_telemetry.get('deploy_submit_offset', 0):.2f}s")
        print("  - Stage transitions:")
        for stage, t_off in deploy_telemetry.get("stage_timestamps", {}).items():
            print(f"      * {stage:<16} at t={t_off:.3f}s")
        print(f"  - Final status:          {deploy_telemetry.get('final_status')}")
        print(f"  - Active container:      {deploy_telemetry.get('active_container')}")

    print("=" * 60)

    out_path = args.out
    if not out_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"deploy_{args.deploy_new_version}" if args.deploy_new_version else "baseline"
        out_path = f"benchmarks/run_{ts}_{suffix}.json"

    serializable_seconds = {}
    for sec, data in sorted(seconds_map.items()):
        lats = data["latencies"]
        serializable_seconds[sec] = {
            "total": data["total"],
            "ok": data["ok"],
            "errors": data["errors"],
            "error_details": dict(data["error_details"]),
            "versions": dict(data["versions"]),
            "p50_latency_ms": round(sorted(lats)[len(lats) // 2], 2) if lats else 0,
            "avg_latency_ms": round(sum(lats) / len(lats), 2) if lats else 0,
            "max_latency_ms": round(max(lats), 2) if lats else 0,
        }

    summary_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "threads": args.threads,
            "duration": args.duration,
            "pause": args.pause,
            "url": args.url,
            "host": args.host,
            "deploy_new_version": args.deploy_new_version,
            "deploy_at": args.deploy_at,
        },
        "metrics": {
            "total_requests": total_reqs,
            "successful_requests": ok_reqs,
            "non_200_requests": non_200_reqs,
            "error_rate_pct": round(non_200_reqs / total_reqs * 100, 3) if total_reqs else 0,
            "rps": round(total_reqs / total_time, 2),
            "breakdown": dict(breakdown),
            "error_seconds": error_seconds,
            "initial_version": initial_version,
            "new_version": new_version_name,
            "first_new_version_offset_sec": round(first_new_version_offset, 3) if first_new_version_offset else None,
            "first_new_version_second": first_new_version_sec,
            "old_after_new_count": len(old_after_new_records),
        },
        "deploy_telemetry": deploy_telemetry,
        "db_events": db_events,
        "seconds_breakdown": serializable_seconds,
    }

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2)

    print(f"\nSaved detailed benchmark data to {out_path}\n")


if __name__ == "__main__":
    main()
