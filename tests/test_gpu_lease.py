"""shared/gpu_lease.py: one holder per GPU pair, lapsing leases, wants, and
two real processes racing for the same pair."""
import importlib.util
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "gpu_lease.py"


def load(tmp_path, monkeypatch):
    monkeypatch.setenv("GPU_LEASE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("gpu_lease", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gl(tmp_path, monkeypatch):
    return load(tmp_path, monkeypatch)


def test_one_holder_per_pair_until_release(gl):
    a = gl.acquire("pair23", "3d-prompt", 5, job_id="j1")
    assert a and a["gpus"] == [2, 3] and a["port"] == 11436
    assert gl.acquire("pair23", "other", 5) is None
    assert gl.holder_of("pair23")["holder"] == "3d-prompt"
    assert gl.acquire("pair01", "other", 5)                  # the other pair is separate
    gl.release(a)
    assert gl.holder_of("pair23") is None
    assert gl.acquire("pair23", "other", 5)


def test_lease_is_capped_and_lapses_without_heartbeat(gl, monkeypatch):
    a = gl.acquire("pair01", "x", 999)
    assert a["expires"] - a["since"] == gl.MAX_MINUTES * 60
    now = time.time()
    monkeypatch.setattr(gl.time, "time", lambda: now + gl.HEARTBEAT_STALE_S + 1)
    assert gl.holder_of("pair01") is None                     # crashed holder: lapsed
    assert not gl.heartbeat(a)
    b = gl.acquire("pair01", "y", 5)                          # cleared by the next taker
    assert b and b["holder"] == "y"
    gl.release(a)                                            # the old holder can't release b
    assert gl.holder_of("pair01")["holder"] == "y"


def test_heartbeat_keeps_it_alive(gl, monkeypatch):
    a = gl.acquire("pair01", "x", 10)
    t = [time.time()]
    monkeypatch.setattr(gl.time, "time", lambda: t[0])
    for _ in range(5):
        t[0] += gl.HEARTBEAT_STALE_S - 5
        assert gl.heartbeat(a)
    assert gl.holder_of("pair01")["holder"] == "x"


def test_a_want_blocks_new_leases_and_shows_to_the_holder(gl, monkeypatch):
    a = gl.acquire("pair23", "3d-prompt", 10)
    gl.want("pair23", "storywriter job 7")
    assert [w["who"] for w in gl.wanted("pair23")] == ["storywriter job 7"]
    gl.release(a)
    assert gl.acquire("pair23", "3d-prompt", 10) is None      # nothing new while Story Writer waits
    gl.unwant("pair23", "storywriter job 7")
    assert gl.acquire("pair23", "3d-prompt", 10)
    gl.want("pair01", "sw")
    now = time.time()
    monkeypatch.setattr(gl.time, "time", lambda: now + gl.WANT_STALE_S + 1)
    assert gl.wanted("pair01") == []                          # a forgotten want lapses


def test_unknown_pair_and_port_mapping(gl):
    with pytest.raises(ValueError):
        gl.acquire("pair99", "x", 1)
    assert gl.pair_for_port(11434) == "pair01" and gl.pair_for_port(11436) == "pair23"
    assert gl.pair_for_port(9999) is None
    assert gl.pair_for_port(11435) == "pair01"                     # GPU 1 in the "work" layout
    assert gl.pair_for_url("http://host.docker.internal:11436/api/chat") == "pair23"
    assert gl.pair_for_url("http://127.0.0.1:11434") == "pair01"
    assert gl.pair_for_url("http://laptop.your-tailnet.ts.net:11434") is None     # another machine
    assert gl.pair_for_url("http://172.17.0.1:11437") is None                   # shared-pc tunnel
    assert gl.pair_for_url("not a url") is None


def test_status_lists_holder_and_wants(gl):
    gl.acquire("pair23", "3d-prompt", 5, job_id="mesh-1")
    gl.want("pair23", "sw")
    s = gl.status()
    assert s["pair23"]["holder"]["job_id"] == "mesh-1" and s["pair23"]["wanted_by"] == ["sw"]
    assert s["pair01"]["holder"] is None
    json.dumps(s)


def _race(lease_dir, n, q):
    import os
    os.environ["GPU_LEASE_DIR"] = lease_dir
    spec = importlib.util.spec_from_file_location("gpu_lease", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    wins, deadline = 0, time.time() + 3        # keep competing while the probe holds and checks
    while time.time() < deadline:
        lease = mod.acquire("pair01", f"p{n}", 1)
        if lease:
            wins += 1
            mod.release(lease)
    q.put((n, wins))


def test_two_processes_never_hold_the_same_pair(tmp_path):
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    held = []
    # Each process takes and releases repeatedly; an overlap would show as a
    # lease file holding the other process's token while one thinks it holds.
    probe = ctx.Process(target=_hold_and_check, args=(str(tmp_path), q))
    probe.start()
    procs = [ctx.Process(target=_race, args=(str(tmp_path), n, q)) for n in range(3)]
    for p in procs:
        p.start()
    for p in procs + [probe]:
        p.join(60)
    while not q.empty():
        held.append(q.get())
    probe_result = [h for h in held if h[0] == "probe"]
    assert probe_result and probe_result[0][1] == "ok", probe_result
    assert sum(h[1] for h in held if h[0] != "probe") > 10      # the racers really competed


def _hold_and_check(lease_dir, q):
    import os
    os.environ["GPU_LEASE_DIR"] = lease_dir
    spec = importlib.util.spec_from_file_location("gpu_lease", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    time.sleep(0.5)                           # let the racers get going first
    deadline = time.time() + 2
    got = None
    while time.time() < deadline and not got:
        got = mod.acquire("pair01", "probe", 1)
    if not got:
        q.put(("probe", "never got it"))
        return
    for _ in range(20):                       # while we hold it, nobody else may
        cur = mod.holder_of("pair01")
        if not cur or cur["token"] != got["token"] or not mod.heartbeat(got):
            q.put(("probe", f"lost it to {cur and cur['holder']}"))
            return
        time.sleep(0.02)
    mod.release(got)
    q.put(("probe", "ok"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
