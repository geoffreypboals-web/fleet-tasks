"""design.py: design jobs (board WP-154). One printed part per design; the size is checked
against the brief; the owner looks before it's kept; a manual is an option. No network:
a fake 3D print tool on a local port, and queue tasks finished by hand."""
import importlib.util
import io
import os
import json
import zipfile
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "fleet.py"


def box_stl(x, y, z):
    """A binary STL whose extent is x × y × z (two triangles are enough for the size)."""
    tris = [((0, 0, 0), (x, 0, 0), (x, y, 0)), ((0, 0, z), (x, y, z), (0, y, z))]
    data = b"\0" * 80 + struct.pack("<I", len(tris))
    for t in tris:
        data += struct.pack("<3f", 0, 0, 1) + b"".join(struct.pack("<3f", *v) for v in t) + b"\0\0"
    return data


class Print3D(BaseHTTPRequestHandler):
    sizes = []          # one size per build, in order
    prompts = []

    def log_message(self, *a):
        pass

    def _out(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/generate":
            req = json.loads(body)
            Print3D.prompts.append(req)
            if "BROKEN" in (req.get("scad") or ""):               # the tool's 422 for code that won't render
                return self._out(422, {"detail": "the given OpenSCAD didn't render: Parser error in line 3"})
            return self._out(200, {"model_id": f"m{len(Print3D.prompts)}", "backend": req.get("backend")})
        if self.path == "/thumbnail":
            return self._out(200, b"\x89PNG fake", "image/png")
        self._out(404, {})

    def do_GET(self):
        if self.path.endswith("/download"):
            n = int(self.path.split("/")[2][1:])
            return self._out(200, box_stl(*Print3D.sizes[min(n, len(Print3D.sizes)) - 1]), "model/stl")
        self._out(404, {})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Print3D)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Print3D.sizes, Print3D.prompts = [], []
    pol = json.loads((SRC.parent / "tests" / "policy.json").read_text())
    pol["tools"] = {"print3d": {"url": f"http://127.0.0.1:{server.server_address[1]}"}}
    pol.setdefault("design", {})["output_dir"] = str(tmp_path / "kept")
    (tmp_path / "policy.json").write_text(json.dumps(pol))
    monkeypatch.setenv("FLEET_POLICY", str(tmp_path / "policy.json"))
    monkeypatch.setenv("FLEET_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("GPU_LEASE_DIR", str(tmp_path / "leases"))
    spec = importlib.util.spec_from_file_location("fleet", SRC)
    fleet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleet)
    monkeypatch.setattr(fleet, "endpoints", lambda fresh=False: [])      # no machine: the shape check is skipped
    idea = fleet.submit("hard", "tabletop accessories")
    finish(fleet, idea, "1. Dice tray. 2. Terrain crate with a lid. 3. A ruined tower.")
    yield fleet, fleet.design_mod(), idea
    server.shutdown()


def finish(fleet, tid, result):
    with fleet.conn() as c:
        c.execute("UPDATE tasks SET status='done', result=? WHERE id=?", (result, tid))


def row(D, did):
    return D.detail(did)


PARTS = {"parts": [
    {"name": "Dice tray", "route": "structural", "product": "", "purpose": "roll dice without knocking models"},
    {"name": "Crate body", "route": "structural", "product": "Terrain crate", "purpose": "holds terrain"},
    {"name": "Crate lid", "route": "structural", "product": "Terrain crate", "purpose": "closes the crate"},
    {"name": "Ruined tower", "route": "artistic", "product": "", "purpose": "line-of-sight blocker"}]}

BRIEF = {"name": "Dice tray", "route": "structural", "purpose": "roll dice", "envelope_mm": [150, 100, 25],
         "print_orientation": "flat", "features": ["felt recess 140 x 90 x 1 mm"], "fits": [], "assumptions": []}


def test_an_idea_becomes_one_design_per_part_and_products_get_an_assembly(env):
    fleet, D, idea = env
    did = D.start(idea)
    assert row(D, did)["stage"] == "parts"
    task = fleet.db().execute("SELECT kind, prompt FROM tasks WHERE id=?", (row(D, did)["task_id"],)).fetchone()
    assert task["kind"] == "design-parts" and "One entry per printed part" in task["prompt"]
    finish(fleet, row(D, did)["task_id"], "Here you go:\n```json\n" + json.dumps(PARTS) + "\n```")
    D.step()
    assert row(D, did)["stage"] == "listed" and "4 parts, 1 products" in row(D, did)["note"]
    rows = {r["name"]: r for r in D.listing()}
    assert set(rows) == {"Dice tray", "Crate body", "Crate lid", "Ruined tower", "Terrain crate", f"parts of #{idea}"}
    crate = rows["Terrain crate"]
    assert crate["stage"] == "assembly"
    assert rows["Crate body"]["parent_id"] == crate["id"] == rows["Crate lid"]["parent_id"]
    assert rows["Dice tray"]["parent_id"] == 0 and rows["Ruined tower"]["route"] == "artistic"


def _to_brief(env):
    fleet, D, idea = env
    did = D.start(idea, "dice tray")
    finish(fleet, row(D, did)["task_id"], json.dumps({"parts": PARTS["parts"][:1]}))
    D.step()
    tray = next(r for r in D.listing() if r["name"] == "Dice tray")["id"]
    D.step()                                                   # starts its brief task
    return fleet, D, tray


def test_a_brief_with_several_parts_or_too_big_is_sent_back(env):
    fleet, D, tray = _to_brief(env)
    finish(fleet, row(D, tray)["task_id"], json.dumps({"parts": [BRIEF, BRIEF]}))
    D.step()
    assert "one part" in row(D, tray)["note"] and row(D, tray)["stage"] == "brief"
    D.step()                                                   # asks again, saying why
    t = fleet.db().execute("SELECT prompt FROM tasks WHERE id=?", (row(D, tray)["task_id"],)).fetchone()
    assert "The last brief was rejected" in t["prompt"]
    finish(fleet, row(D, tray)["task_id"], json.dumps(dict(BRIEF, envelope_mm=[300, 300, 20])))
    D.step()
    assert "doesn't fit the bed" in row(D, tray)["note"]


def test_structural_part_is_built_size_checked_rebuilt_and_waits_for_the_owner(env):
    fleet, D, tray = _to_brief(env)
    finish(fleet, row(D, tray)["task_id"], json.dumps(BRIEF))
    D.step()
    assert row(D, tray)["stage"] == "prompt"
    D.step()                                                   # starts the spec task
    t = fleet.db().execute("SELECT kind, prompt FROM tasks WHERE id=?", (row(D, tray)["task_id"],)).fetchone()
    assert t["kind"] == "design-prompt" and "150 × 100 × 25 mm" in t["prompt"]
    finish(fleet, row(D, tray)["task_id"], "Dice tray, 150 x 100 x 25 mm, walls 2 mm, felt recess ...")
    D.step()
    assert row(D, tray)["stage"] == "build"
    Print3D.sizes = [(150, 25, 100), (100, 150, 25)]           # first build stands up; second is right (turned)
    D.step()
    d = row(D, tray)
    assert d["stage"] == "prompt" and d["tries"] == 1 and "built 150 × 25 × 100 mm" in d["feedback"]
    assert Print3D.prompts[0]["backend"] == "openscad" and Print3D.prompts[0]["use_templates"] is False
    D.step()                                                   # rewrites the spec with the numbers
    t = fleet.db().execute("SELECT prompt FROM tasks WHERE id=?", (row(D, tray)["task_id"],)).fetchone()
    assert "built 150 × 25 × 100 mm" in t["prompt"] and "walls 2 mm" in t["prompt"]
    finish(fleet, row(D, tray)["task_id"], "Dice tray, lying flat: 150 x 100 x 25 mm ...")
    D.step()
    D.step()
    d = row(D, tray)
    assert d["stage"] == "look" and "matches the brief" in d["note"] and d["render"]
    assert (fleet.STATE / "designs" / str(tray) / "part.stl").exists()
    with pytest.raises(ValueError, match="say what"):
        D.change(tray, " ")
    assert row(D, tray)["saved"] == "" and D.zip_of(tray) is None          # nothing kept before approval
    D.approve(tray, by="owner@example.com")
    assert row(D, tray)["stage"] == "approved"
    assert [e["event"] for e in fleet.read_ledger()][-1] == "design-approve"
    kept = D.output_dir() / f"idea-{row(D, tray)['source']}" / f"Dice tray - {tray}"
    assert row(D, tray)["saved"] == str(kept) and fleet.read_ledger()[-1]["saved"] == str(kept)
    assert sorted(p.name for p in kept.iterdir()) == ["Dice tray.stl", "about.txt", "brief.json", "preview.png", "spec.txt"]
    assert (kept / "Dice tray.stl").read_bytes() == (fleet.STATE / "designs" / str(tray) / "part.stl").read_bytes()
    assert "100 × 150 × 25 mm" in (kept / "about.txt").read_text() and "lying flat" in (kept / "spec.txt").read_text()
    name, data = D.zip_of(tray)
    assert name == f"Dice tray - {tray}.zip"
    assert f"Dice tray - {tray}/Dice tray.stl" in zipfile.ZipFile(io.BytesIO(data)).namelist()


def test_a_spec_with_options_is_refused(env):
    fleet, D, tray = _to_brief(env)
    finish(fleet, row(D, tray)["task_id"], json.dumps(BRIEF))
    D.step()
    D.step()
    finish(fleet, row(D, tray)["task_id"], "Option A: a round tray.\nOption B: a square tray.")
    D.step()
    d = row(D, tray)
    assert d["stage"] == "prompt" and "options" in d["note"] and not Print3D.prompts


def test_a_manual_is_an_option_once_approved(env):
    fleet, D, tray = _to_brief(env)
    with pytest.raises(ValueError, match="isn't approved"):
        D.want_manual(tray)
    with fleet.conn() as c:
        c.execute("UPDATE designs SET stage='approved', brief=?, spec='walls 2 mm', size='[150, 100, 25]' WHERE id=?",
                  (json.dumps(BRIEF), tray))
    D.want_manual(tray, by="owner@example.com")
    with pytest.raises(ValueError, match="already on its way"):
        D.want_manual(tray)
    D.step()
    d = row(D, tray)
    t = fleet.db().execute("SELECT kind, prompt FROM tasks WHERE id=?", (d["manual_task"],)).fetchone()
    assert d["manual"] == "writing" and t["kind"] == "design-manual"
    assert "## Build steps" in t["prompt"] and "walls 2 mm" in t["prompt"] and "Safety" in t["prompt"]
    finish(fleet, d["manual_task"], "## What you need\n| Part | Count |\n## Build steps\n1. Glue the felt.")
    D.step()
    assert row(D, tray)["manual"] == "done" and "Glue the felt" in D.manual_text(tray)


def test_a_products_manual_waits_for_all_its_parts(env):
    fleet, D, idea = env
    did = D.start(idea)
    finish(fleet, row(D, did)["task_id"], json.dumps(PARTS))
    D.step()
    rows = {r["name"]: r["id"] for r in D.listing()}
    D.want_manual(rows["Terrain crate"])
    with fleet.conn() as c:
        c.execute("UPDATE designs SET stage='approved', brief='{}' WHERE id=?", (rows["Crate body"],))
    D.step()
    assert row(D, rows["Terrain crate"])["manual"] == "wanted"            # the lid isn't approved yet
    with fleet.conn() as c:
        c.execute("UPDATE designs SET stage='approved', brief='{}' WHERE id=?", (rows["Crate lid"],))
    D.step()
    assert row(D, rows["Terrain crate"])["manual"] == "writing"
    assert "2 of 2 parts approved" in row(D, rows["Terrain crate"])["note"]


def test_sizes_and_checks(env):
    _, D, _ = env
    assert D.stl_size(box_stl(10, 20, 3)) == [10, 20, 3]
    ascii_stl = b"solid x\n facet normal 0 0 1\n outer loop\n vertex 0 0 0\n vertex 76.2 0 0\n vertex 0 76.2 4\n endloop\n endfacet\nendsolid\n"
    assert D.stl_size(ascii_stl) == [76.2, 76.2, 4]
    assert D.size_ok([100, 150, 25], [150, 100, 25]) == ""                 # turned on the bed is fine
    assert "80.2" in D.size_ok([76.2, 20, 80.2], [76.2, 76.2, 4])        # 2026-10-28: the standing L-marker
    assert D.fits_bed([230, 100, 20], [220, 220, 250]) is False
    assert D.multi_design("## Part 1: Tray\n...\n## Part 2: Lid\n...") and D.multi_design("Option A: x")
    assert D.multi_design("**Part A** tray\n**Part B** lid")
    assert D.multi_design("## Features\n1. a recess\n2. a lip") == ""
    # 2026-10-28: one part described feature by feature is one design
    assert D.multi_design("**Part Name:** Wing Guard\n### 1. Base Plate\n...\n### 2. Support Arms\n**2. Bridge**") == ""
    with pytest.raises(ValueError):
        D.parts_from('{"parts": []}')


def test_a_spec_too_long_for_the_build_tool_is_shortened_first(env):
    """2026-10-28: 32 builds failed on the 3D print tool's 2000-character cap (specs ran 2-44k)."""
    fleet, D, tray = _to_brief(env)
    finish(fleet, row(D, tray)["task_id"], json.dumps(BRIEF))
    D.step()
    D.step()
    t = fleet.db().execute("SELECT prompt FROM tasks WHERE id=?", (row(D, tray)["task_id"],)).fetchone()
    assert "At most 6000 characters" in t["prompt"]
    finish(fleet, row(D, tray)["task_id"], "Dice tray 150 x 100 x 25 mm. " + "x" * 7000)
    D.step()
    d = row(D, tray)
    assert d["stage"] == "prompt" and "characters" in d["feedback"] and not Print3D.prompts
    D.step()
    t = fleet.db().execute("SELECT prompt FROM tasks WHERE id=?", (row(D, tray)["task_id"],)).fetchone()
    assert "cut the explanations" in t["prompt"] and "Dice tray 150 x 100" in t["prompt"]


def test_a_products_parts_are_kept_together_with_its_manual(env):
    fleet, D, idea = env
    did = D.start(idea)
    finish(fleet, row(D, did)["task_id"], json.dumps(PARTS))
    D.step()
    rows = {r["name"]: r["id"] for r in D.listing()}
    for part in ("Crate body", "Crate lid"):
        (fleet.STATE / "designs" / str(rows[part])).mkdir(parents=True, exist_ok=True)
        (fleet.STATE / "designs" / str(rows[part]) / "render.png").write_bytes(b"\x89PNG fake")
        (fleet.STATE / "designs" / str(rows[part]) / "part.stl").write_bytes(box_stl(120, 80, 40))
        with fleet.conn() as c:
            c.execute("UPDATE designs SET stage='look', brief='{}', spec='walls 2 mm', size='[120, 80, 40]' WHERE id=?",
                      (rows[part],))
        D.approve(rows[part])
    crate = D.output_dir() / f"idea-{idea}" / "Terrain crate"
    assert sorted(p.name for p in crate.iterdir()) == [f"Crate body - {rows['Crate body']}", f"Crate lid - {rows['Crate lid']}"]
    assert row(D, rows["Terrain crate"])["saved"] == str(crate)
    D.want_manual(rows["Terrain crate"])
    D.step()
    finish(fleet, row(D, rows["Terrain crate"])["manual_task"], "## Build steps\n1. Put the lid on.")
    D.step()
    manual = (crate / "manual.md").read_text()
    assert "Put the lid on" in manual and f"](Crate lid - {rows['Crate lid']}/preview.png)" in manual
    names = zipfile.ZipFile(io.BytesIO(D.zip_of(rows["Terrain crate"])[1])).namelist()
    assert "Terrain crate/manual.md" in names and f"Terrain crate/Crate body - {rows['Crate body']}/Crate body.stl" in names
    name, data = D.zip_of(did)                                   # the parts list row: the whole idea
    assert name == f"idea-{idea}.zip"
    assert f"idea-{idea}/Terrain crate/manual.md" in zipfile.ZipFile(io.BytesIO(data)).namelist()


def test_kept_names_are_safe(env):
    _, D, _ = env
    assert D._slug('Dice "Tower" / v2: big?') == "Dice Tower v2 big"
    assert D._slug("../..") == "part" and D._slug("") == "part"
    assert len(D._slug("x" * 200)) == 60


def _to_build(env):
    fleet, D, tray = _to_brief(env)
    finish(fleet, row(D, tray)["task_id"], json.dumps(BRIEF))
    D.step()
    D.step()
    finish(fleet, row(D, tray)["task_id"], "Dice tray, lying flat: 150 x 100 x 25 mm, felt recess 140 x 90 x 1 mm")
    D.step()
    assert row(D, tray)["stage"] == "build"
    Print3D.sizes = [(150, 100, 25)] * 3
    return fleet, D, tray


def test_the_shape_check_sends_a_part_with_missing_features_back(env, monkeypatch):
    """Owner 2026-10-28: parts the right size but missing features (a crate closed on top, a dial
    with no numbers) are caught by a vision model before they reach the owner."""
    fleet, D, tray = _to_build(env)
    monkeypatch.setattr(fleet, "endpoints", lambda fresh=False: [
        {"name": "big", "url": "http://x", "model": "qwen3.6:27b", "tier": "large", "up": True, "loaded": ["qwen3.6:27b"]}])
    calls = []

    def vision(ep, system, prompt, json_out=False, timeout=900, on_text=None, images=None):
        calls.append((ep["model"], len(images or []), "felt recess" in prompt))
        seen = "missing" if len(calls) == 1 else "yes"
        return json.dumps({"features": [{"feature": "felt recess 140 x 90 x 1 mm", "seen": seen},
                                         {"feature": "0.4 mm chamfer", "seen": "unclear"}],
                           "wrong": ["closed on top"] if len(calls) == 1 else []})
    monkeypatch.setattr(fleet, "ollama", vision)
    D.step()
    d = row(D, tray)
    assert calls == [("qwen3.6:27b", 3, True)]                           # three views, the brief's features
    assert d["stage"] == "prompt" and "missing: felt recess" in d["feedback"] and "closed on top" in d["feedback"]
    assert (fleet.STATE / "designs" / str(tray) / "render-bottom.png").exists()
    D.step()
    finish(fleet, row(D, tray)["task_id"], "Dice tray, open top, felt recess 140 x 90 x 1 mm, 150 x 100 x 25 mm")
    D.step()
    D.step()
    d = row(D, tray)
    assert d["stage"] == "look" and "shape check: 1 of 2 features seen" in d["note"] and "missing" not in d["note"]


def test_no_vision_model_loaded_means_no_check_and_nothing_loaded(env, monkeypatch):
    fleet, D, tray = _to_build(env)
    monkeypatch.setattr(fleet, "endpoints", lambda fresh=False: [
        {"name": "big", "url": "http://x", "model": "qwen3.6:27b", "tier": "large", "up": True, "loaded": ["gemma3:27b"]}])
    monkeypatch.setattr(fleet, "ollama", lambda *a, **k: pytest.fail("would load a model over another job's"))
    D.step()
    d = row(D, tray)
    assert d["stage"] == "look" and "shape check skipped" in d["note"]


class ImageService(BaseHTTPRequestHandler):
    answers = []           # (code, body) per request, in order
    asked = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        ImageService.asked.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
        code, body = ImageService.answers.pop(0)
        self.send_response(code)
        self.send_header("Content-Type", "image/png" if code == 200 else "application/json")
        self.end_headers()
        self.wfile.write(body)


def test_an_artistic_part_gets_a_picture_when_the_gpus_are_free(env, monkeypatch):
    fleet, D, idea = env
    server = ThreadingHTTPServer(("127.0.0.1", 0), ImageService)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ImageService.answers = [(503, b'{"error": "waiting for qwen3.6:27b to leave pair23"}'), (200, b"\x89PNG tower")]
    ImageService.asked = []
    pol = json.loads(fleet.POLICY.read_text())
    pol["tools"]["image"] = {"url": f"http://127.0.0.1:{server.server_address[1]}"}
    fleet.POLICY.write_text(json.dumps(pol))
    did = D.start(idea, "ruined tower")
    finish(fleet, row(D, did)["task_id"], json.dumps({"parts": PARTS["parts"][3:]}))
    D.step()
    tower = next(r for r in D.listing() if r["name"] == "Ruined tower")["id"]
    D.step()
    finish(fleet, row(D, tower)["task_id"], json.dumps(dict(BRIEF, name="Ruined tower", route="artistic",
                                                            envelope_mm=[80, 80, 120])))
    D.step()
    D.step()
    t = fleet.db().execute("SELECT prompt FROM tasks WHERE id=?", (row(D, tower)["task_id"],)).fetchone()
    assert "image_prompt" in t["prompt"]
    finish(fleet, row(D, tower)["task_id"], json.dumps({"image_prompt": "a ruined stone tower, grey",
                                                        "meshy_prompt": "ruined tower, 80 mm base"}))
    D.step()
    assert row(D, tower)["stage"] == "image"
    D.step()                                                  # GPUs busy: it waits, nothing fails
    d = row(D, tower)
    assert d["stage"] == "image" and "picture waits" in d["note"]
    D.step()
    assert len(ImageService.asked) == 1                        # not asked again before IMAGE_RETRY_S
    D._image_next.clear()
    D.step()
    d = row(D, tower)
    assert d["stage"] == "look" and d["render"] and ImageService.asked[-1]["prompt"] == "a ruined stone tower, grey"
    assert (fleet.STATE / "designs" / str(tower) / "picture.png").read_bytes() == b"\x89PNG tower"
    server.shutdown()


def test_a_3d_tool_that_is_down_costs_no_try(env):
    """2026-10-28: redeploying the 3D print tool made every waiting build fail at once,
    each losing a try and its spec. Down or restarting: the build waits instead."""
    fleet, D, tray = _to_build(env)
    good = D.print3d_url()
    pol = json.loads(open(os.environ["FLEET_POLICY"]).read())
    pol["tools"]["print3d"]["url"] = "http://127.0.0.1:9"              # nothing listens there
    open(os.environ["FLEET_POLICY"], "w").write(json.dumps(pol))
    D.step()
    d = row(D, tray)
    assert d["stage"] == "build" and d["tries"] == 0 and "isn't answering" in d["note"]
    pol["tools"]["print3d"]["url"] = good
    open(os.environ["FLEET_POLICY"], "w").write(json.dumps(pol))
    D.step()
    assert row(D, tray)["stage"] == "build" and not Print3D.prompts     # waits TOOL_RETRY_S before asking again
    D._tool_next[0] = 0
    D.step()
    assert row(D, tray)["stage"] == "look" and len(Print3D.prompts) == 1


def test_briefs_and_specs_go_to_gemini_without_asking_and_specs_carry_only_the_brief(env):
    """Owner 2026-10-29: Gemini writes briefs and specs (local briefs contradicted themselves),
    with the CLI's own model choice; a spec task stays under Gemini's prompt cap."""
    fleet, D, tray = _to_brief(env)
    pol = json.loads(open(os.environ["FLEET_POLICY"]).read())
    pol["frontier"]["gemini"]["enabled"] = True
    open(os.environ["FLEET_POLICY"], "w").write(json.dumps(pol))
    assert fleet.providers_for("design-brief") == ["gemini"]           # never Copilot: it bills overage
    brief_task = fleet.db().execute("SELECT * FROM tasks WHERE id=?", (row(D, tray)["task_id"],)).fetchone()
    assert brief_task["kind"] == "design-brief" and brief_task["frontier_ok"]
    assert fleet.gate(brief_task)[0] == "gemini"                       # no approval card
    assert fleet.paid_choice("gemini", brief_task)[0]["model_arg"] == "auto"
    finish(fleet, brief_task["id"], json.dumps(BRIEF))
    D.step()
    D.step()
    spec_task = fleet.db().execute("SELECT * FROM tasks WHERE id=?", (row(D, tray)["task_id"],)).fetchone()
    assert spec_task["kind"] == "design-prompt" and spec_task["frontier_ok"] and not spec_task["uses"]
    assert "felt recess" in spec_task["prompt"] and "tabletop accessories" not in spec_task["prompt"]


def test_a_brief_whose_envelope_doesnt_hold_its_features_is_sent_back(env):
    D = env[1]
    ok = dict(BRIEF, features=["two pins spaced 160 mm apart", "felt recess 140 x 90 x 1 mm"])
    assert D.check_brief(json.dumps(ok))["envelope_mm"] == [150, 100, 25]
    bad = dict(BRIEF, envelope_mm=[60, 60, 45], features=["main rail 210 mm long"])
    with pytest.raises(ValueError, match="210 mm"):
        D.check_brief(json.dumps(bad))


def test_a_spec_that_finds_its_brief_contradictory_sends_the_design_back_to_the_brief(env):
    """2026-10-29, #5: the brief said a 15 mm rail in a 150 mm deep envelope; the spec
    'adjusted' the rail to 150 mm and the part came out a slab."""
    fleet, D, tray = _to_brief(env)
    finish(fleet, row(D, tray)["task_id"], json.dumps(BRIEF))
    D.step()
    D.step()
    finish(fleet, row(D, tray)["task_id"], "Rail: 150 mm. Note: Width adjusted to 150 mm to match envelope constraint.")
    D.step()
    d = row(D, tray)
    assert d["stage"] == "brief" and "contradicts itself" in d["feedback"] and not d["spec"]
    D.step()                                                            # a new brief, told why
    task = fleet.db().execute("SELECT prompt FROM tasks WHERE id=?", (row(D, tray)["task_id"],)).fetchone()
    assert "adjusted to 150 mm" in task["prompt"]
    finish(fleet, row(D, tray)["task_id"], json.dumps(BRIEF))
    D.step()
    D.step()
    finish(fleet, row(D, tray)["task_id"], "CONFLICT: the envelope is 25 mm high but the walls are 40 mm")
    D.step()
    assert row(D, tray)["stage"] == "brief" and "walls are 40 mm" in row(D, tray)["feedback"]


def test_rebrief_sends_structural_designs_back_to_a_fresh_brief(env):
    fleet, D, tray = _to_build(env)
    D.step()
    assert row(D, tray)["stage"] == "look"
    assert D.rebrief_candidates() == [tray]
    D.rebrief(tray, "keep it under 20 mm high")
    d = row(D, tray)
    assert d["stage"] == "brief" and d["tries"] == 0 and not d["spec"] and d["feedback"] == "keep it under 20 mm high"
    D.step()
    task = fleet.db().execute("SELECT kind, prompt FROM tasks WHERE id=?", (row(D, tray)["task_id"],)).fetchone()
    assert task["kind"] == "design-brief" and "under 20 mm" in task["prompt"]


def test_rebrief_with_no_id_from_the_terminal_redoes_every_waiting_structural_design(env):
    """2026-10-29: 'design rebrief' with no N stopped at 'needs an id'."""
    import types
    fleet, D, tray = _to_build(env)
    D.step()
    assert row(D, tray)["stage"] == "look"
    assert fleet.design_cli(types.SimpleNamespace(action="rebrief", id=None, words="")) == 0
    assert row(D, tray)["stage"] == "brief"
    with pytest.raises(SystemExit, match="needs an id"):
        fleet.design_cli(types.SimpleNamespace(action="approve", id=None, words=""))



def test_the_spec_writer_writes_the_openscad_and_the_tool_renders_it_as_written(env):
    """Owner 2026-10-31: Gemini writes the OpenSCAD with the spec (qwen3.6 turned correct specs into
    wrong CSG); the 3D print tool renders it (its #34). Code that won't render goes back to the writer."""
    fleet, D, tray = _to_brief(env)
    finish(fleet, row(D, tray)["task_id"], json.dumps(BRIEF))
    D.step()
    D.step()
    ask = fleet.db().execute("SELECT prompt FROM tasks WHERE id=?", (row(D, tray)["task_id"],)).fetchone()["prompt"]
    assert "```openscad block" in ask and "overlap by 0.5 mm" in ask
    code = "difference() {\n  cube([150, 100, 25]);\n  translate([5, 5, 24]) cube([140, 90, 1.5]);\n}"
    finish(fleet, row(D, tray)["task_id"], "Dice tray, lying flat: 150 x 100 x 25 mm, felt recess 140 x 90 x 1 mm.\n\n"
           "```openscad\nBROKEN\n```\n\nOops, the fixed one:\n```openscad\n" + code + "\n```")
    D.step()
    d = row(D, tray)
    assert d["stage"] == "build" and d["scad"] == code and "```" not in d["spec"] and "felt recess" in d["spec"]
    Print3D.sizes = [(150, 100, 25)] * 3
    D.step()
    assert Print3D.prompts[-1]["scad"] == code and "felt recess" in Print3D.prompts[-1]["prompt"]
    assert row(D, tray)["stage"] == "look"
    assert (D.folder(tray) / "part.scad").read_text() == code


def test_openscad_that_wont_render_goes_back_to_its_writer_with_the_error_and_the_code(env):
    fleet, D, tray = _to_brief(env)
    finish(fleet, row(D, tray)["task_id"], json.dumps(BRIEF))
    D.step()
    D.step()
    finish(fleet, row(D, tray)["task_id"], "Tray 150 x 100 x 25 mm.\n```openscad\ncube([150, 100, 25]); BROKEN\n```")
    D.step()
    Print3D.sizes = [(150, 100, 25)]
    D.step()
    d = row(D, tray)
    assert d["stage"] == "prompt" and d["tries"] == 1 and "didn't render" in d["feedback"]
    D.step()
    ask = fleet.db().execute("SELECT prompt FROM tasks WHERE id=?", (row(D, tray)["task_id"],)).fetchone()["prompt"]
    assert "Parser error" in ask and "cube([150, 100, 25]); BROKEN" in ask


def test_a_spec_without_code_still_builds_with_the_tools_own_writer(env):
    fleet, D, tray = _to_build(env)
    D.step()
    assert "scad" not in Print3D.prompts[-1] and row(D, tray)["stage"] == "look"
