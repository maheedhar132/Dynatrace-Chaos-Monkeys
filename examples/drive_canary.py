"""Drive the reference canary service so it generates real errors/latency.

Pairs with canary_service.py: it hammers `/work` for a while with a chosen
failure rate and latency, so an OneAgent-monitored canary service actually
degrades. That real degradation is the fault the TRACE-gated monkeys test the
detection of — run this while the monkey's mutation is in place, inside your
`--oracle-wait` window.

    python examples/drive_canary.py http://localhost:8099 --fail 0.5 --latency 800 --secs 120

For EVENT / METRIC / LOG monkeys you don't need this at all — `troop run` drives
those itself, or use `troop drive --kind metric` to inject on demand.
"""

from __future__ import annotations

import argparse
import time
import urllib.request


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate real load/errors against the canary service.")
    ap.add_argument("base", help="canary service base URL, e.g. http://localhost:8099")
    ap.add_argument("--fail", type=float, default=0.5, help="fraction of requests that 500 (0..1)")
    ap.add_argument("--latency", type=int, default=0, help="per-request latency to induce, ms")
    ap.add_argument("--secs", type=int, default=60, help="how long to drive, seconds")
    ap.add_argument("--rps", type=float, default=5.0, help="requests per second")
    args = ap.parse_args()

    url = f"{args.base.rstrip('/')}/work?fail={args.fail}&latency={args.latency}"
    end = time.time() + args.secs
    ok = err = 0
    interval = 1.0 / max(0.1, args.rps)
    print(f"driving {url} for {args.secs}s at ~{args.rps} rps ...")
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                ok += 1 if r.status == 200 else 0
                err += 0 if r.status == 200 else 1
        except Exception:
            err += 1
        time.sleep(interval)
    print(f"done: {ok} ok, {err} errors. Dynatrace should have seen the canary service degrade.")


if __name__ == "__main__":
    main()
