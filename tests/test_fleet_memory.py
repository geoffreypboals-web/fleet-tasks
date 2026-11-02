"""fleet-tasks shared memory (stacks/fleet-tasks/memory.py and fleet.py's
remember/recall, board WP-152 step 2): storing, the secret refusal, search
by meaning and by words, filling vectors later, never evicting, the
auto-save of a kind's answers, and the page and MCP paths. The embedding
model is stubbed: a vector of letter counts, so similar words score high."""
import importlib.util
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent


def fake_vec(text):
    text = text.split(": ", 1)[-1].lower()              # drop nomic's search_document:/search_query: prefix
    return [float(text.count(ch)) for ch in "abcdefghijklmnopqrstuvwxyz"]


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("GPU_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("FLEET_ALLOWED_LOGINS", "owner@example.com")
    spec = importlib.util.spec_from_file_location("fleet_web", HERE / "web.py")
    web = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web)
    f = web.fleet
    f.web = web
    f.calls = []

    def embed(texts, query=False, timeout=60):
        f.calls.append((list(texts), query))
        return None if f.offline else [fake_vec(t) for t in texts]
    f.offline = False
    monkeypatch.setattr(f, "embed", embed)
    return f


def test_remember_and_recall_by_meaning(fleet):
    a, vec = fleet.remember("Keep two GPU lanes of qwen3.6 on gpu-server", "decision", ["gpu", "GPU-server"])
    assert vec
    fleet.remember("Bananas are yellow", "fruit")
    hits = fleet.recall("why two gpu lanes on gpu-server", k=1)
    assert hits[0]["id"] == a and hits[0]["how"] == "meaning"
    assert hits[0]["tags"] == ["gpu", "gpu-server"]
    assert fleet.calls[-1][1] is True                   # the query is embedded as a query


def test_secrets_are_refused_and_nothing_is_stored(fleet):
    for bad in ("token ghp_" + "a" * 30, "key AKIA" + "B" * 16, "-----BEGIN OPENSSH PRIVATE KEY-----"):
        with pytest.raises(ValueError, match="secret"):
            fleet.remember(bad)
    c = fleet.mem()
    assert fleet.MEMORY.count(c)["notes"] == 0
    c.close()
    with pytest.raises(ValueError):
        fleet.remember("   ")
    with pytest.raises(ValueError):
        fleet.remember("x" * (fleet.NOTE_MAX_CHARS + 1))


def test_offline_notes_are_found_by_words_then_filled_in(fleet):
    fleet.offline = True
    nid, vec = fleet.remember("Spoolman tracks filament spools")
    assert not vec
    hits = fleet.recall("filament spools")
    assert hits[0]["id"] == nid and hits[0]["how"] == "words"
    assert fleet.recall("nothing like it at all") == []
    assert fleet.fill_vectors() == 0                    # still offline
    fleet.offline = False
    assert fleet.fill_vectors() == 1
    assert fleet.recall("filament spools")[0]["how"] == "meaning"


def test_tag_filter_and_forget(fleet):
    a, _ = fleet.remember("printer note", tags=["printer"])
    b, _ = fleet.remember("printer note too", tags=["other"])
    assert [h["id"] for h in fleet.recall("printer note", k=5, tags=["printer"])] == [a]
    assert fleet.forget(b) and not fleet.forget(b)


def test_a_kind_with_remember_keeps_its_answers(fleet, tmp_path, monkeypatch):
    pol = json.loads((HERE / "tests" / "policy.json").read_text())
    pol["kinds"]["summarize"]["remember"] = True
    (tmp_path / "policy.json").write_text(json.dumps(pol))
    monkeypatch.setattr(fleet, "POLICY", tmp_path / "policy.json")
    keep = fleet.submit("summarize", "x")
    skip = fleet.submit("draft", "y")
    with fleet.conn() as c:
        fleet.finish(c, keep, "done", "the summary", "m")
        fleet.finish(c, skip, "done", "the draft", "m")
    c = fleet.mem()
    notes = fleet.MEMORY.latest(c)
    c.close()
    assert [n["text"] for n in notes] == ["the summary"]
    assert notes[0]["task_id"] == keep and not notes[0]["has_vec"]     # filled later, not on the worker's time


def test_the_embedding_model_doesnt_make_a_machine_busy(fleet):
    ep = {"name": "gpu-server-gpu01", "url": "http://127.0.0.1:11434", "model": "qwen3.6:27b", "tier": "large", "up": True}
    assert fleet.free(dict(ep, loaded=["nomic-embed-text:latest"]))
    assert fleet.free(dict(ep, loaded=["nomic-embed-text:latest", "qwen3.6:27b"]))
    assert not fleet.free(dict(ep, loaded=["mistral-small3.2:24b"]))


def test_embedding_never_goes_where_another_job_is_loaded(fleet, monkeypatch):
    base = {"url": "http://127.0.0.1:1", "model": "qwen3.6:27b", "tier": "large", "up": True}
    eps = [dict(base, name="gpu-server-gpu23", loaded=["mistral-small3.2:24b"]),     # Story Writer's model
           dict(base, name="gpu-server-gpu01", loaded=["qwen3.6:27b"])]
    monkeypatch.setattr(fleet, "endpoints", lambda fresh=False: [dict(e) for e in eps])
    assert fleet.embed_endpoint()["name"] == "gpu-server-gpu01"
    eps[1]["loaded"] = ["gemma3:27b"]
    assert fleet.embed_endpoint() is None
    eps[1]["loaded"] = ["nomic-embed-text:latest"]
    eps.append(dict(base, name="gpu-server-gpu99", loaded=[]))                       # not in embed_on
    assert fleet.embed_endpoint()["name"] == "gpu-server-gpu01"


def test_page_memory_search_and_delete(fleet):
    web = fleet.web
    nid, _ = fleet.remember("the page can find this note")
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    h = {"Tailscale-User-Login": "owner@example.com", "X-Fleet": "1", "Content-Type": "application/json"}
    try:
        with urllib.request.urlopen(urllib.request.Request(base + "/api/memory", headers=h)) as r:
            d = json.load(r)
        assert d["total"] == 1 and d["notes"][0]["id"] == nid
        with urllib.request.urlopen(urllib.request.Request(base + "/api/memory?q=find+note", headers=h)) as r:
            assert json.load(r)["notes"][0]["id"] == nid
        req = urllib.request.Request(base + f"/api/memory/delete/{nid}", b"{}", h)
        with urllib.request.urlopen(req) as r:
            assert json.load(r)["ok"]
        with urllib.request.urlopen(urllib.request.Request(base + "/api/memory", headers=h)) as r:
            assert json.load(r)["total"] == 0
    finally:
        server.shutdown()


def test_mcp_remember_and_recall(tmp_path, monkeypatch, fleet):
    spec = importlib.util.spec_from_file_location("fleet_mcp", HERE / "mcp.py")
    mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp)
    monkeypatch.setattr(mcp.fleet, "embed", lambda texts, query=False, timeout=60: [fake_vec(t) for t in texts])
    mcp._submits.clear()
    say = lambda name, **a: mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": name, "arguments": a}})["result"]
    r = say("fleet_remember", text="Owner wants Copilot only as last fallback", tags=["decision"])
    assert not r.get("isError") and "saved" in r["content"][0]["text"]
    r = say("fleet_recall", query="copilot fallback")
    assert "last fallback" in r["content"][0]["text"] and "claude session (MCP)" in r["content"][0]["text"]
    r = say("fleet_remember", text="here is sk-" + "x" * 30)
    assert r.get("isError") and "secret" in r["content"][0]["text"]
