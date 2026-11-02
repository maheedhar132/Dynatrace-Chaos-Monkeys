"""A tiny canary service you can break on command — the reference workload.

The TRACE-gated monkeys (gibbon, mangabey, gelada, saki, capuchin, langur) need a
*real* service, monitored by OneAgent, that actually errors or slows down — a
synthetic event can't move a response-time baseline or a failure rate. This is
that service, in ~40 lines of stdlib, with nothing to install.

Run it on a host that has OneAgent installed, inside your canary management zone:

    python examples/canary_service.py --port 8099

Then make it misbehave while a monkey's mutation is in place:

    # 50% of requests return 500, each taking ~800ms:
    python examples/drive_canary.py http://localhost:8099 --fail 0.5 --latency 800 --secs 120

OneAgent sees a real service called "canary-service" degrade; that is the fault
gibbon / mangabey / gelada / saki are testing the detection of. Tag the host (or
the process group) `chaos-canary` so it lands in your canary zone, and point the
monkey at that scope. Nothing here touches Dynatrace directly — it just behaves
badly on purpose so Dynatrace has something real to notice.
"""

from __future__ import annotations

import argparse
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Live knobs, set by query string on /work?fail=..&latency=.. (or via drive_canary).
STATE = {"fail": 0.0, "latency_ms": 0}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        q = parse_qs(urlparse(self.path).query)
        if "fail" in q:
            STATE["fail"] = _clamp(float(q["fail"][0]), 0.0, 1.0)
        if "latency" in q:
            STATE["latency_ms"] = max(0, int(q["latency"][0]))

        if STATE["latency_ms"]:
            time.sleep(STATE["latency_ms"] / 1000.0)

        if random.random() < STATE["fail"]:
            self._send(500, "canary-service: injected error\n")   # a real 5xx OneAgent sees
        else:
            self._send(200, "canary-service: ok\n")

    def _send(self, code: int, body: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args) -> None:
        pass  # quiet


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def main() -> None:
    ap = argparse.ArgumentParser(description="A canary service you can break on command.")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    print(f"canary-service on http://{args.host}:{args.port}  "
          f"(GET /work?fail=0.5&latency=800 to misbehave)")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
