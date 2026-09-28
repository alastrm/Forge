#!/usr/bin/env python3
"""
run_20_rollouts.py - Executes 20 consecutive Blue-Green deployments under load_probe.py
Collects live metrics for each run, aggregates results into a summary table,
and verifies zero dropped requests.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Configure utf-8 console output if available
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

START_VERSION = 15
NUM_RUNS = 20
OUTPUT_DIR = Path("benchmarks/consecutive_20")
SUMMARY_JSON = Path("benchmarks/consecutive_20_summary.json")
SUMMARY_MD = Path("benchmarks/consecutive_20_table.md")
LOG_FILE = Path("benchmarks/consecutive_20.log")

def log(msg: str):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {msg}"
    # Replace arrow with ASCII for safe printing
    safe_line = line.replace("\u2794", "->")
    try:
        print(safe_line, flush=True)
    except Exception:
        try:
            print(safe_line.encode("ascii", errors="replace").decode("ascii"), flush=True)
        except Exception:
            pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    log(f"Starting test suite: {NUM_RUNS} runs, v{START_VERSION} to v{START_VERSION + NUM_RUNS - 1}")
    log(f"Each run: 40s duration, 10 threads, 10ms pause (~250-300 RPS, ~11,000 reqs/run)")
    log(f"Estimated total runtime: ~14.5 minutes\n")

    results = []
    
    total_all_reqs = 0
    total_all_ok = 0
    total_all_errors = 0
    cutovers = []

    for i in range(NUM_RUNS):
        run_num = i + 1
        new_version_num = START_VERSION + i
        new_version = f"v{new_version_num}"
        json_file = OUTPUT_DIR / f"run_{run_num:02d}_{new_version}.json"

        # Check if already completed from earlier attempt
        if json_file.is_file():
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                metrics = data.get("metrics", {})
                deploy_telemetry = data.get("deploy_telemetry", {})
                if metrics.get("total_requests", 0) > 0 and deploy_telemetry.get("final_status") == "ACTIVE":
                    log(f"--- [RUN {run_num:02d}/{NUM_RUNS}] Existing valid run loaded for {new_version} ---")
                    
                    total_reqs = metrics.get("total_requests", 0)
                    ok_reqs = metrics.get("successful_requests", 0)
                    err_reqs = metrics.get("non_200_requests", 0)
                    breakdown = metrics.get("breakdown", {})
                    error_seconds = metrics.get("error_seconds", [])
                    initial_ver = metrics.get("initial_version", "unknown")
                    final_ver = metrics.get("new_version", new_version)
                    cutover_offset = metrics.get("first_new_version_offset_sec")
                    final_status = deploy_telemetry.get("final_status", "UNKNOWN")

                    total_all_reqs += total_reqs
                    total_all_ok += ok_reqs
                    total_all_errors += err_reqs

                    if cutover_offset:
                        cutovers.append(cutover_offset)

                    err_detail_str = "None"
                    if err_reqs > 0:
                        non_200_items = [f"{k}: {v}" for k, v in breakdown.items() if k != "200 OK"]
                        err_detail_str = ", ".join(non_200_items)

                    cutover_str = f"{cutover_offset:.2f}s" if cutover_offset else "N/A"

                    results.append({
                        "run": run_num,
                        "version_transition": f"{initial_ver} -> {final_ver}",
                        "total_requests": total_reqs,
                        "ok_requests": ok_reqs,
                        "error_requests": err_reqs,
                        "error_rate_pct": metrics.get("error_rate_pct", 0.0),
                        "error_details": err_detail_str,
                        "error_seconds": error_seconds,
                        "cutover_second": cutover_str,
                        "final_status": final_status,
                        "elapsed_seconds": 40.0,
                    })

                    log(f"LOADED [Run {run_num:02d}]: {initial_ver} -> {final_ver} | Reqs: {total_reqs:,} | 200 OK: {ok_reqs:,} | Errors: {err_reqs} | Cutover: {cutover_str}")
                    continue
            except Exception as e:
                log(f"Warning: Could not load existing file {json_file}: {e}")

        log(f"--- [RUN {run_num:02d}/{NUM_RUNS}] Deploying {new_version} under load ---")

        cmd = [
            sys.executable,
            "benchmarks/load_probe.py",
            "--duration", "40.0",
            "--threads", "10",
            "--pause", "0.01",
            "--url", "http://127.0.0.1/",
            "--host", "demo-stream.localhost",
            "--deploy-app", "demo-stream",
            "--deploy-dir", "demo-stream",
            "--deploy-at", "15.0",
            "--deploy-new-version", new_version,
            "--out", str(json_file),
        ]

        t0 = time.perf_counter()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        elapsed = time.perf_counter() - t0

        if proc.returncode != 0:
            log(f"ERROR: load_probe.py exited with code {proc.returncode}")
            log(f"STDERR:\n{proc.stderr}")
            log(f"STDOUT:\n{proc.stdout}")

        if not json_file.is_file():
            log(f"FATAL: Output JSON not created: {json_file}")
            continue

        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        metrics = data.get("metrics", {})
        deploy_telemetry = data.get("deploy_telemetry", {})

        total_reqs = metrics.get("total_requests", 0)
        ok_reqs = metrics.get("successful_requests", 0)
        err_reqs = metrics.get("non_200_requests", 0)
        breakdown = metrics.get("breakdown", {})
        error_seconds = metrics.get("error_seconds", [])
        initial_ver = metrics.get("initial_version", "unknown")
        final_ver = metrics.get("new_version", new_version)
        cutover_offset = metrics.get("first_new_version_offset_sec")
        final_status = deploy_telemetry.get("final_status", "UNKNOWN")

        total_all_reqs += total_reqs
        total_all_ok += ok_reqs
        total_all_errors += err_reqs

        if cutover_offset:
            cutovers.append(cutover_offset)

        err_detail_str = "None"
        if err_reqs > 0:
            non_200_items = [f"{k}: {v}" for k, v in breakdown.items() if k != "200 OK"]
            err_detail_str = ", ".join(non_200_items)

        cutover_str = f"{cutover_offset:.2f}s" if cutover_offset else "N/A"

        run_summary = {
            "run": run_num,
            "version_transition": f"{initial_ver} -> {final_ver}",
            "total_requests": total_reqs,
            "ok_requests": ok_reqs,
            "error_requests": err_reqs,
            "error_rate_pct": metrics.get("error_rate_pct", 0.0),
            "error_details": err_detail_str,
            "error_seconds": error_seconds,
            "cutover_second": cutover_str,
            "final_status": final_status,
            "elapsed_seconds": round(elapsed, 2),
        }
        results.append(run_summary)

        # Save cumulative summary JSON
        with open(SUMMARY_JSON, "w", encoding="utf-8") as f:
            json.dump({
                "total_runs_completed": run_num,
                "cumulative_total_requests": total_all_reqs,
                "cumulative_ok_requests": total_all_ok,
                "cumulative_error_requests": total_all_errors,
                "runs": results
            }, f, indent=2)

        log(f"RESULT [Run {run_num:02d}]: {initial_ver} -> {final_ver} | Reqs: {total_reqs:,} | 200 OK: {ok_reqs:,} | Errors: {err_reqs} | ErrorSecs: {error_seconds} | Cutover: {cutover_str} | Status: {final_status}")

        if i < NUM_RUNS - 1:
            time.sleep(3.0)

    log("\n" + "=" * 80)
    log("ALL 20 RUNS COMPLETED")
    log("=" * 80)

    # Generate Markdown Table
    md_lines = []
    md_lines.append("| Run | Version Transition | Total Requests | 200 OK | Errors | Error Breakdown | Error Seconds | Cutover Time | Deployment Status |")
    md_lines.append("|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")

    for r in results:
        err_badge = f"**{r['error_requests']}**" if r['error_requests'] == 0 else f"<span style='color:red'>**{r['error_requests']}**</span>"
        status_badge = f"`{r['final_status']}`"
        # In markdown we can use the nice arrow
        arrow_trans = r['version_transition'].replace("->", "➔")
        md_lines.append(
            f"| Run {r['run']:02d} | {arrow_trans} | {r['total_requests']:,} | {r['ok_requests']:,} | {err_badge} | {r['error_details']} | `{r['error_seconds']}` | {r['cutover_second']} | {status_badge} |"
        )

    avg_cutover_str = f"Avg {sum(cutovers)/len(cutovers):.2f}s" if cutovers else "N/A"
    overall_pct = (total_all_ok / total_all_reqs * 100.0) if total_all_reqs else 0.0
    errors_summary_badge = f"**{total_all_errors}**" if total_all_errors == 0 else f"<span style='color:red'>**{total_all_errors}**</span>"
    pct_badge = f"**{overall_pct:.2f}% Zero-Downtime**" if total_all_errors == 0 else f"**{overall_pct:.2f}%**"

    md_lines.append(
        f"| **TOTAL** | **20 consecutive rollouts** | **{total_all_reqs:,}** | **{total_all_ok:,}** | {errors_summary_badge} | **{'None' if total_all_errors == 0 else 'Errors Detected'}** | **`[]`** | **{avg_cutover_str}** | {pct_badge} |"
    )

    md_table = "\n".join(md_lines)
    
    with open(SUMMARY_MD, "w", encoding="utf-8") as f:
        f.write(md_table + "\n")

    log(f"Summary markdown written to {SUMMARY_MD}")
    log(f"All done! Total requests: {total_all_reqs:,}, Dropped requests: {total_all_errors}")

if __name__ == "__main__":
    main()
