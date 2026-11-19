#!/usr/bin/env python3
"""Traffic generator and alert simulator for the SRE Task Manager (standard library only).

Replaces the old per-minute CronJob and trigger_alerts.sh.

Modes:
  normal   healthy mixed traffic (create/list/update/delete + one invalid request per loop)
  errors   enables chaos error injection (60% HTTP 500) -> triggers HighErrorRate
  latency  enables chaos latency (1000 ms) -> triggers HighResponseLatency
  down     scales the deployment to 0 replicas with kubectl -> triggers TaskManagerDown

Chaos settings (and the replica count) are restored when the script ends or is interrupted.

Examples:
  python3 scripts/generate_traffic.py --url http://127.0.0.1:30007 --mode normal --duration 60
  python3 scripts/generate_traffic.py --url http://127.0.0.1:30007 --mode errors --duration 150
  python3 scripts/generate_traffic.py --url http://127.0.0.1:30007 --mode latency --duration 150
  python3 scripts/generate_traffic.py --mode down --duration 90
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEPLOYMENT = ("-n", "application", "deployment/sre-task-manager")
TIMEOUT_SECONDS = 5


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def call(base_url, method, path, payload=None):
    """Send one request and return the HTTP status (0 when the app is unreachable)."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base_url + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            response.read()
            return response.status
    except urllib.error.HTTPError as err:
        return err.code
    except (urllib.error.URLError, TimeoutError):
        return 0


def set_chaos(base_url, error_rate, latency_ms):
    payload = {"error_rate": error_rate, "latency_ms": latency_ms}
    status = call(base_url, "POST", "/api/chaos", payload)
    if status != 200:
        sys.exit(f"Could not set chaos (HTTP {status}). Is ENABLE_CHAOS=true and the URL correct?")


def create_task(base_url):
    request = urllib.request.Request(
        base_url + "/api/tasks",
        data=json.dumps({"title": "synthetic check"}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            return json.load(response).get("id")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def traffic_loop(base_url, duration, interval, full_cycle):
    """Send requests until `duration` seconds have passed."""
    end = time.monotonic() + duration
    sent = 0
    while time.monotonic() < end:
        if full_cycle:
            task_id = create_task(base_url)
            call(base_url, "GET", "/api/tasks")
            if task_id is not None:
                call(base_url, "PUT", f"/api/tasks/{task_id}", {"done": True})
            call(base_url, "POST", "/api/tasks", {"title": ""})  # invalid on purpose
            if task_id is not None:
                call(base_url, "DELETE", f"/api/tasks/{task_id}")
            sent += 5
        else:
            call(base_url, "GET", "/api/tasks")
            sent += 1
        time.sleep(interval)
    log(f"Sent about {sent} requests in {duration}s")


def kubectl_scale(replicas):
    command = ["kubectl", "scale", *DEPLOYMENT, f"--replicas={replicas}"]
    subprocess.run(command, check=True)  # nosec B603 B607


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:30007", help="app base URL")
    parser.add_argument("--mode", choices=["normal", "errors", "latency", "down"], default="normal")
    parser.add_argument("--duration", type=int, default=60, help="seconds (alerts need > 2 min)")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between loops")
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        parser.error("--url must be an http(s) URL such as http://127.0.0.1:30007")
    args.url = args.url.rstrip("/")
    return args


def main():
    args = parse_args()
    log(f"mode={args.mode} url={args.url} duration={args.duration}s")
    try:
        if args.mode == "normal":
            traffic_loop(args.url, args.duration, args.interval, full_cycle=True)
        elif args.mode == "errors":
            set_chaos(args.url, 0.6, 0)
            traffic_loop(args.url, args.duration, args.interval, full_cycle=False)
        elif args.mode == "latency":
            set_chaos(args.url, 0, 1000)
            traffic_loop(args.url, args.duration, 0.2, full_cycle=False)
        else:
            log("Scaling sre-task-manager to 0 replicas")
            kubectl_scale(0)
            time.sleep(args.duration)
    except KeyboardInterrupt:
        log("Interrupted")
    finally:
        if args.mode in ("errors", "latency"):
            log("Resetting chaos")
            call(args.url, "POST", "/api/chaos", {"error_rate": 0, "latency_ms": 0})
        elif args.mode == "down":
            log("Restoring 1 replica")
            kubectl_scale(1)


if __name__ == "__main__":
    main()
