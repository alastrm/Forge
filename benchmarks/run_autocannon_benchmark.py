#!/usr/bin/env python3
"""
Orchestrates high-throughput autocannon benchmark with live Forge Blue-Green deployment.
"""

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    print("=" * 60)
    print("FORGE HIGH-THROUGHPUT AUTOCANNON BENCHMARK")
    print("Connections: 100")
    print("Duration:    45s")
    print("Target:      http://127.0.0.1/ (Host: demo-stream.localhost)")
    print("Deploy:      v14 at t = 15.0s")
    print("=" * 60)

    # 1. Update version to v14 in demo-stream/server.py
    server_py = Path("demo-stream/server.py")
    code = server_py.read_text(encoding="utf-8")
    code = re.sub(r'APP_VERSION\s*=\s*["\'][^"\']+["\']', 'APP_VERSION = "v14"', code)
    server_py.write_text(code, encoding="utf-8")
    print("[1/4] Updated demo-stream/server.py to APP_VERSION = 'v14'")

    # 2. Launch autocannon
    cmd_autocannon = [
        "cmd.exe", "/c",
        "npx", "autocannon",
        "-c", "100",
        "-d", "45",
        "-H", "Host: demo-stream.localhost",
        "http://127.0.0.1/"
    ]

    t_start = time.monotonic()
    print("[2/4] Starting autocannon process...")
    proc_ac = subprocess.Popen(
        cmd_autocannon,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    deploy_output = []
    deploy_timing = {}

    def deploy_worker():
        target_time = t_start + 15.0
        remaining = target_time - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

        t_trigger = time.monotonic() - t_start
        print(f"\n>>> [3/4] Triggering deploy of v14 at t={t_trigger:.2f}s <<<\n", flush=True)
        deploy_timing["trigger_sec"] = round(t_trigger, 2)

        cmd_deploy = [sys.executable, "-m", "forge.cli", "deploy", "--app", "demo-stream", "demo-stream"]
        p_dep = subprocess.Popen(
            cmd_deploy,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in p_dep.stdout:
            sys.stdout.write(f"  [forge] {line}")
            sys.stdout.flush()
            deploy_output.append(line)
        p_dep.wait()
        deploy_timing["exit_code"] = p_dep.returncode
        deploy_timing["completed_sec"] = round(time.monotonic() - t_start, 2)
        print(f"\n>>> Deploy completed at t={deploy_timing['completed_sec']:.2f}s (exit_code={p_dep.returncode}) <<<\n", flush=True)


    t_dep = threading.Thread(target=deploy_worker, daemon=True)
    t_dep.start()

    # Wait for autocannon to complete
    ac_stdout, _ = proc_ac.communicate()
    t_total = time.monotonic() - t_start
    t_dep.join(timeout=10.0)

    # 1. First save file
    out_file = Path("benchmarks/autocannon_high_throughput.txt")
    full_log = (
        f"Forge High-Throughput Autocannon Benchmark\n"
        f"Total Elapsed Time: {t_total:.2f}s\n"
        f"Deploy Triggered at: {deploy_timing.get('trigger_sec')}s\n"
        f"Deploy Completed at: {deploy_timing.get('completed_sec')}s\n"
        f"\n--- Deployment Output ---\n"
        f"{''.join(deploy_output)}\n"
        f"--- Autocannon Raw Output ---\n"
        f"{ac_stdout}\n"
    )
    out_file.write_text(full_log, encoding="utf-8")
    print(f"Saved benchmark results to {out_file}")

    print("\n" + "=" * 60)
    print("AUTOCANNON RESULT:")
    print("=" * 60)
    try:
        print(ac_stdout)
    except Exception:
        # Fallback to buffer write or ascii replace if console font lacks chars
        sys.stdout.buffer.write(ac_stdout.encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()

if __name__ == "__main__":
    main()
