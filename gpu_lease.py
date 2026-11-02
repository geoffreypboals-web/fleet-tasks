# ==============================================================================
# FILE: stacks/fleet-tasks/gpu_lease.py  (copy of shared/gpu_lease.py; keep in sync)
# PURPOSE: Check out one of gpu-server's GPU pairs for a job that isn't Ollama's
#          (board WP-63; plan 3d-printing-model-prompt
#          docs/plans/organic-mesh-backends-2026-10-07-plan.md section 6).
#          Copy this file into an app. No dependencies.
#
#          A lease is GPU_LEASE_DIR/<pair>.json: {holder, job_id, token, gpus,
#          port, since, expires, heartbeat}. Rules:
#          - One holder per pair. acquire() returns None while a live lease
#            exists or while anyone wants the pair (a want file).
#          - A lease lapses at `expires` (at most MAX_MINUTES after it was
#            taken) or when its heartbeat is older than HEARTBEAT_STALE_S, so a
#            crashed holder can't keep a pair. A lapsed lease is cleared by the
#            next acquire().
#          - AI Story Writer has priority: before a job starts on a leased pair
#            it calls want(); the holder sees wanted() at its next step,
#            finishes the piece in progress, releases, and takes nothing new.
#            Wants lapse after WANT_STALE_S unless renewed.
#          - fleet-tasks and Story Writer never load a model on a leased pair
#            (they call holder_of() before using it).
#          Every change happens under flock(<pair>.lock), so two processes
#          (or two containers sharing the folder) can't both take a pair, and
#          clearing a lapsed lease can't remove a fresh one.
# ==============================================================================
import fcntl
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

LEASE_DIR = Path(os.environ.get("GPU_LEASE_DIR", "/data/stacks/gpu-leases"))
# gpu_server_gpu_pairing.sh: Ollama :11434 runs on GPUs 0+1 ("power" layout; in
# "work" :11434 is GPU 0 and :11435 GPU 1), :11436 on GPUs 2+3.
PAIRS: Dict[str, Dict] = {"pair01": {"gpus": [0, 1], "port": 11434, "ports": [11434, 11435]},
                          "pair23": {"gpus": [2, 3], "port": 11436, "ports": [11436]}}
MAX_MINUTES = 20
HEARTBEAT_STALE_S = 60
WANT_STALE_S = 15 * 60


# Hosts that mean "gpu-server itself" (seen from the host or from a container on it).
# Other machines also serve Ollama on 11434, so the port alone isn't enough.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "host.docker.internal", "gpu-server",
               "gpu-server.your-tailnet.ts.net", "100.64.0.1"} | set(filter(None, os.environ.get("GPU_LEASE_HOSTS", "").split(",")))


def pair_for_port(port: int) -> Optional[str]:
    """The pair an Ollama port runs on (11434 -> pair01), or None."""
    return next((p for p, v in PAIRS.items() if int(port) in v["ports"]), None)


def pair_for_url(url: str) -> Optional[str]:
    """The pair an Ollama URL runs on, only when it points at gpu-server itself
    (laptop:11434 is not pair01), or None."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        port = u.port
    except ValueError:
        return None
    if not port or (u.hostname or "").lower() not in LOCAL_HOSTS:
        return None
    return pair_for_port(port)


def _check(pair: str) -> None:
    if pair not in PAIRS:
        raise ValueError(f"unknown GPU pair {pair!r}; one of {sorted(PAIRS)}")


@contextmanager
def _locked(pair: str):
    _check(pair)
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LEASE_DIR / f"{pair}.lock", "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def _live(lease: Optional[dict], now: float) -> bool:
    return bool(lease) and now < lease.get("expires", 0) and now - lease.get("heartbeat", 0) < HEARTBEAT_STALE_S


def _wants(pair: str, now: float) -> List[dict]:
    out = []
    for f in LEASE_DIR.glob(f"{pair}.want.*"):
        w = _read(f)
        if w and now - w.get("at", 0) < WANT_STALE_S:
            out.append(w)
        else:
            f.unlink(missing_ok=True)       # a lapsed want
    return out


def holder_of(pair: str) -> Optional[dict]:
    """The live lease on `pair`, or None when it's free."""
    _check(pair)
    lease = _read(LEASE_DIR / f"{pair}.json")
    return lease if _live(lease, time.time()) else None


def acquire(pair: str, holder: str, minutes: float, job_id: str = "") -> Optional[dict]:
    """Take `pair` for at most `minutes` (capped at MAX_MINUTES). Returns the
    lease (keep it for heartbeat/release), or None when the pair is held or
    wanted. The caller still checks that no Story Writer job runs on it."""
    now = time.time()
    with _locked(pair):
        path = LEASE_DIR / f"{pair}.json"
        if _live(_read(path), now) or _wants(pair, now):
            return None
        lease = {"pair": pair, "holder": holder, "job_id": job_id, "token": secrets.token_hex(8),
                 "gpus": PAIRS[pair]["gpus"], "port": PAIRS[pair]["port"], "since": now,
                 "expires": now + min(float(minutes), MAX_MINUTES) * 60, "heartbeat": now}
        _write(path, lease)
        return lease


def heartbeat(lease: dict) -> bool:
    """Keep `lease` alive (every 15 s or so). False when it's no longer ours
    (lapsed and cleared, or released): stop using the GPUs."""
    with _locked(lease["pair"]):
        path = LEASE_DIR / f"{lease['pair']}.json"
        cur = _read(path)
        if not cur or cur.get("token") != lease["token"] or not _live(cur, time.time()):
            return False
        cur["heartbeat"] = time.time()
        _write(path, cur)
        return True


def release(lease: dict) -> None:
    """Give the pair back. Safe to call twice, or after the lease lapsed."""
    with _locked(lease["pair"]):
        path = LEASE_DIR / f"{lease['pair']}.json"
        cur = _read(path)
        if cur and cur.get("token") == lease["token"]:
            path.unlink(missing_ok=True)


def want(pair: str, who: str) -> None:
    """Ask the holder to give `pair` back soon (AI Story Writer's priority).
    Renew it while waiting; it lapses after WANT_STALE_S."""
    with _locked(pair):
        _write(LEASE_DIR / f"{pair}.want.{_safe(who)}", {"who": who, "at": time.time()})


def unwant(pair: str, who: str) -> None:
    with _locked(pair):
        (LEASE_DIR / f"{pair}.want.{_safe(who)}").unlink(missing_ok=True)


def wanted(pair: str) -> List[dict]:
    """Live wants on `pair` (empty when nobody is waiting for it)."""
    with _locked(pair):
        return _wants(pair, time.time())


def status() -> Dict[str, dict]:
    """Each pair's holder and wants, for status pages (SuiteControl's GPU card)."""
    now = time.time()
    out = {}
    for pair in PAIRS:
        lease = holder_of(pair)
        out[pair] = {"gpus": PAIRS[pair]["gpus"], "port": PAIRS[pair]["port"],
                     "holder": lease and {k: lease[k] for k in ("holder", "job_id", "since", "expires")},
                     "wanted_by": [w["who"] for w in wanted(pair)], "checked": now}
    return out


def _safe(who: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in who)[:60] or "anon"
