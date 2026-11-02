"""fleet-tasks MCP server (stacks/fleet-tasks/mcp.py, board WP-152 step 1):
the JSON-RPC handshake, each tool against a temp queue, the hourly cap, and
that nothing reachable through MCP can approve or turn on paid AI.
No Ollama: endpoints are stubbed."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

LIBENV = {k: v for k, v in os.environ.items() if k == "LD_LIBRARY_PATH"}  # the runner's Python needs its libpython

SRC = Path(__file__).resolve().parent.parent / "mcp.py"


@pytest.fixture()
def mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("GPU_LEASE_DIR", str(tmp_path / "leases"))
    spec = importlib.util.spec_from_file_location("fleet_mcp", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.fleet, "endpoints", lambda fresh=False: [
        {"name": "big", "url": "x", "model": "qwen3.6:27b", "tier": "large", "up": True, "loaded": ["qwen3.6:27b"]},
        {"name": "sleeper", "url": "x", "model": "llama3.2:3b", "tier": "tiny", "up": False, "loaded": []}])
    mod._submits.clear()
    return mod


def rpc(mcp, method, params=None, mid=1):
    return mcp.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def call(mcp, name, **args):
    res = rpc(mcp, "tools/call", {"name": name, "arguments": args})["result"]
    return res["content"][0]["text"], bool(res.get("isError"))


def row(mcp, tid):
    with mcp.fleet.conn() as c:
        return dict(c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def test_initialize_echoes_a_known_protocol_and_lists_tools(mcp):
    init = rpc(mcp, "initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert init["protocolVersion"] == "2025-03-26" and "tools" in init["capabilities"]
    assert rpc(mcp, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == mcp.PROTOCOLS[0]
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in rpc(mcp, "tools/list")["result"]["tools"]}
    assert names == {"fleet_submit", "fleet_split", "fleet_status", "fleet_result", "fleet_nodes", "fleet_waiting",
                     "fleet_remember", "fleet_recall"}


def test_tool_list_stays_small(mcp):
    """Every Claude session pays for this text; the plan's cap is 1,500 tokens (about 4 characters each)."""
    size = len(json.dumps(rpc(mcp, "tools/list")["result"]))
    assert size / 4 < 1500, size


def test_no_tool_can_approve_or_enable_paid_ai(mcp):
    names = {t["name"] for t in mcp.tools()}
    assert not any(w in n for n in names for w in ("approve", "deny", "frontier", "unlock", "stop", "forget", "delete"))
    for t in mcp.tools():
        assert "frontier" not in t["inputSchema"]["properties"]
        assert "frontier" not in t["inputSchema"]["properties"].get("tier", {}).get("enum", [])
    text, err = call(mcp, "fleet_submit", kind="draft", prompt="x", frontier=True)
    assert not err
    tid = int(text.split("#")[1].split()[0])
    assert row(mcp, tid)["frontier_ok"] == 0
    text, err = call(mcp, "fleet_submit", kind="draft", prompt="x", tier="frontier")
    assert err and "owner" in text


def test_submit_marks_the_task_and_status_reads_it(mcp):
    text, err = call(mcp, "fleet_submit", kind="summarize", prompt="sum this", check=True)
    assert not err and "queued" in text
    tid = int(text.split("#")[1].split()[0])
    r = row(mcp, tid)
    assert r["kind"] == "summarize" and r["check_result"] == 1 and "through MCP" in r["notes"]
    one, _ = call(mcp, "fleet_status", id=tid)
    assert f"#{tid} summarize" in one and "waiting for a free machine" in one
    many, _ = call(mcp, "fleet_status")
    assert f"#{tid}" in many


def test_bad_input_is_an_error_result_not_a_crash(mcp):
    for args in ({"kind": "nope", "prompt": "x"}, {"kind": "draft", "prompt": "  "}, {"kind": "draft"},
                 {"kind": "draft", "prompt": "x", "paths": ["a"]}):
        text, err = call(mcp, "fleet_submit", **args)
        assert err, args
    text, err = call(mcp, "fleet_status", id=999)
    assert err and "no task" in text
    assert rpc(mcp, "tools/call", {"name": "fleet_nothing"})["error"]["code"] == -32602
    assert rpc(mcp, "no/such")["error"]["code"] == -32601


def test_hourly_submit_cap(mcp, monkeypatch):
    monkeypatch.setattr(mcp, "SUBMITS_PER_HOUR", 2)
    assert not call(mcp, "fleet_submit", kind="draft", prompt="a")[1]
    assert not call(mcp, "fleet_submit", kind="draft", prompt="b")[1]
    text, err = call(mcp, "fleet_submit", kind="draft", prompt="c")
    assert err and "last hour" in text


def test_result_done_waiting_and_still_running(mcp):
    f = mcp.fleet
    tid = f.submit("draft", "x")
    text, _ = call(mcp, "fleet_result", id=tid, timeout=0)
    assert "still" in text
    with f.conn() as c:
        f.finish(c, tid, "done", "the answer", "big")
    text, _ = call(mcp, "fleet_result", id=tid, timeout=0)
    assert "the answer" in text and "big" in text
    wid = f.submit("hard", "y")
    with f.conn() as c:
        f.finish(c, wid, "needs_approval")
    text, _ = call(mcp, "fleet_result", id=wid, timeout=0)
    assert "owner" in text
    assert row(mcp, wid)["status"] == "needs_approval"      # waiting never cancels or approves


def test_long_results_are_cut(mcp, monkeypatch):
    monkeypatch.setattr(mcp, "RESULT_MAX_CHARS", 50)
    f = mcp.fleet
    tid = f.submit("draft", "x")
    with f.conn() as c:
        f.finish(c, tid, "done", "z" * 500, "big")
    text, _ = call(mcp, "fleet_result", id=tid, timeout=0)
    assert "more characters" in text and len(text) < 300


def test_split_job(mcp):
    text, err = call(mcp, "fleet_split", kind="summarize", prompt="sum each", text="one\n\ntwo", part_chars=3)
    assert not err and "split into 2 parts" in text
    pid = int(text.split("#")[1].split()[0])
    assert row(mcp, pid)["status"] == "split"


def test_nodes_and_waiting(mcp):
    text, _ = call(mcp, "fleet_nodes")
    assert "big (large, qwen3.6:27b): free" in text and "sleeper" in text and "paid AI total" in text
    assert call(mcp, "fleet_waiting")[0] == "nothing waits for the owner's OK"
    f = mcp.fleet
    wid = f.submit("hard", "please think hard")
    with f.conn() as c:
        f.finish(c, wid, "needs_approval")
        f.note(c, wid, "hard may use claude only with the owner's OK")
    text, _ = call(mcp, "fleet_waiting")
    assert f"#{wid} hard" in text and "Only the owner" in text


def test_stdio_round_trip(tmp_path):
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    out = subprocess.run([sys.executable, str(SRC)], input="\n".join(json.dumps(m) for m in msgs) + "\nnot json\n",
                         capture_output=True, text=True, timeout=60,
                         env={"FLEET_STATE": str(tmp_path / "s"), "GPU_LEASE_DIR": str(tmp_path / "l"), "PATH": "/usr/bin:/bin", **LIBENV})
    replies = [json.loads(x) for x in out.stdout.splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[0]["result"]["serverInfo"]["name"] == "fleet"
    assert len(replies[1]["result"]["tools"]) == 8
    assert replies[2]["error"]["code"] == -32700
