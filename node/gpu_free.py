#!/usr/bin/env python3
"""gpu_free.py -- tell fleet-tasks how busy this machine's GPU is (board WP-131).

Ollama's /api/ps only lists its own models, so on a shared PC (the owner's
daily machine, shared-pc) fleet-tasks can't see a game, Emby or another
program on the GPU. This answers GET /gpu with what nvidia-smi reports:

  {"gpus": [{"util_pct": 12, "mem_used_mb": 2100, "mem_total_mb": 8188}]}

fleet-tasks gives the machine work only while at least gpu_free_min_pct of
the GPU is free (owner, 2026-10-23: 25% for shared-pc; CPU and RAM
don't count). Read-only: it runs nvidia-smi and nothing else, and it takes
no input. Standard library only. Bind it to the machine's tailnet address
so only the tailnet can reach it:

  python gpu_free.py --host 100.x.y.z [--port 11439]

Install on a node only with the owner's OK (WP-131 plan, decision 4), next
to Ollama, the same way WP-26's node setup runs Ollama.
"""
import argparse
import ipaddress
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QUERY = ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]


def parse(csv_text):
    """nvidia-smi CSV (one GPU per line) -> list of dicts."""
    gpus = []
    for line in csv_text.strip().splitlines():
        util, used, total = (int(float(x.strip())) for x in line.split(","))
        gpus.append({"util_pct": util, "mem_used_mb": used, "mem_total_mb": total})
    return gpus


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # Windows: no console flash per call (owner, 2026-10-28)


def read_gpus():
    out = subprocess.run(QUERY, capture_output=True, text=True, timeout=10, check=True, creationflags=NO_WINDOW)
    return parse(out.stdout)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):   # polled every 20 s; stay quiet
        pass

    def do_GET(self):
        if self.path != "/gpu":
            self.send_error(404)
            return
        try:
            body, code = {"gpus": read_gpus()}, 200
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            body, code = {"error": e.__class__.__name__}, 503     # fleet-tasks then treats the GPU as busy
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


TAILNET = ipaddress.ip_network("100.64.0.0/10")


def tailnet_host(value):
    """argparse type: only a Tailscale address, so the probe is never on the LAN or 0.0.0.0."""
    try:
        if ipaddress.ip_address(value) in TAILNET:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"{value} isn't a tailnet address (100.64.0.0/10); see `tailscale ip -4`")


def main():
    ap = argparse.ArgumentParser(description="GPU busy-ness for fleet-tasks (read-only)")
    ap.add_argument("--host", required=True, type=tailnet_host,
                    help="this machine's tailnet address (100.x.y.z), so only the tailnet can reach it")
    ap.add_argument("--port", type=int, default=11439)
    a = ap.parse_args()
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
