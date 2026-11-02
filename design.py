#!/usr/bin/env python3
"""design.py -- design jobs: from an idea to a checked 3D part, one printed part per job (board WP-154).

Owner rules (2026-10-28): one printed part per design; every candidate is its own
design (nobody drops the rest); a product made of parts gets a separate assembly
step once its parts are approved; structural parts are built in OpenSCAD through
the 3D print tool, artistic ones go through a picture the owner approves first.

A design moves through stages (designs.stage):

  parts     a local task lists the candidate parts of an idea (JSON) -> one design per part
  brief     a local task writes one part's brief: purpose, its size (envelope_mm), features
  prompt    a local task writes the build spec (structural) or the image and Meshy prompts (artistic)
  build     the 3D print tool builds it with OpenSCAD; the size is checked against the brief
            (a miss goes back to `prompt` with the numbers, up to MAX_TRIES); a preview is rendered
  look      waits for the owner: Approve, or Change with words (back to `prompt`)
  approved  done (structural); an artistic part waits for the image generator and Meshy steps
  image     artistic: a picture from its image prompt (image/image.py, FLUX.1 schnell), then `look`
  assembly  a product: waits until all its parts are approved (the assembly step is next to build)
  listed    a `parts` design whose parts were listed (it's only the list)
  manual    (an option, designs.manual) the owner asks for a build manual on an approved part or a
            product: a local task writes it from the approved briefs and specs (manual.md)
  failed, cancelled

Kept files (owner, 2026-10-28): the working files stay in the state folder; Approve
copies a part's STL, preview, spec and brief to the output folder (policy.json
design.output_dir, default ~/designs) as idea-<task>/<product>/<part - id>/, and a
manual lands next to them. The page shows that folder and offers it as a zip.

The thinking stages are ordinary queue tasks (kinds design-parts, design-brief,
design-prompt): local first, paid AI only with the owner's OK, like any task.
The step() loop advances one design at a time; fleet.py runs it on a thread.
"""
import io
import base64
import json
import os
import re
import shutil
import struct
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

F = None                     # the fleet module (bind() sets it)
MAX_TRIES = 3                # builds whose size is off before the owner sees it anyway
SPEC_CHARS = 6000            # longest build spec sent to the 3D print tool (its MAX_PROMPT_LENGTH must be above)
BED = [220, 220, 250]        # default printer bed (policy.json design.bed_mm overrides)
OUTPUT = "~/designs"         # where approved parts are kept (policy.json design.output_dir overrides)
FINAL = ("approved", "listed", "failed", "cancelled")

ROUTES = ("structural", "artistic")
ROUTE_NOTE = ("structural = cases, boxes, dividers, trays, racks, rulers, gauges, jigs, clips, dials, markers, "
              "brackets: exact shapes built in OpenSCAD from a written spec. artistic = sculpted terrain, ruins, "
              "creatures, decorative pieces: made by Meshy (AI 3D) from a picture.")

PARTS_ASK = """List every candidate printed part for the idea{focus} in the earlier answer, for our 3D print tool.

Rules:
- One entry per printed part. A product made of several parts (a crate and its lid; a ruler and its joiner) lists each part on its own, all with the same "product" name; a single-part product has "product": "".
- Every candidate is its own entry: variants and options are separate entries, not one entry with choices.
- {routes}
- Leave out things that are bought, not printed (magnets, screws), except as part of a printed part's purpose.

Answer with JSON only, no other text:
{{"parts": [{{"name": "short name", "route": "structural" or "artistic", "product": "product name or empty", "purpose": "one sentence: what it does for the player"}}]}}"""

BRIEF_ASK = """Write the brief for ONE printed part, for our 3D print tool: "{name}" ({route}).
Purpose: {purpose}
{product}
Rules:
- This one part only: no alternatives, no options, no other parts (they have their own briefs).
- envelope_mm is the part's overall size as printed: [length X, width Y, height Z], Z up, lying in its print orientation. It is exactly the box around all the features together: work it out from them and check every feature fits inside it. A thin divider is thin; never pad the envelope.
- It must fit the printer bed {bed} mm (X, Y, Z).
- Give every size that matters for fit: bases, magnets (e.g. 6x2 mm discs with 0.15 mm press-fit allowance), mating parts, clearances (0.2-0.3 mm), minimum wall 1.2 mm.

Answer with JSON only, no other text:
{{"name": "...", "route": "{route}", "purpose": "...", "envelope_mm": [x, y, z], "print_orientation": "...", "features": ["each feature with its sizes in mm"], "fits": ["what it must fit or mate with, with sizes"], "assumptions": ["what to check"]}}"""

SPEC_ASK = """Write the OpenSCAD-ready build spec for this ONE part, from its brief:

{brief}

Rules:
- One part, one design: no alternatives, no options, no "optional" extras, no other parts.
- Units mm. The part lies in +X, +Y, +Z with its lowest corner at the origin, as printed (Z up), and its overall size is exactly {env} (X × Y × Z).
- Name every feature with its size and its position from the origin: holes, pockets (magnet pockets open to the bottom face, with depth), engravings (depth, text, height), chamfers, fillets.
- Walls and floors at least 1.2 mm; clearances 0.2-0.3 mm where parts fit together.
- If the brief contradicts itself (the envelope doesn't fit the features, two sizes for one thing), change nothing: answer only "CONFLICT: " and what contradicts what.
- The spec: plain sentences and lists, no essay. At most {limit} characters: sizes and positions only, not why.
- Then the OpenSCAD for exactly this spec, in one ```openscad block after it: one difference() with every cutter (holes, pockets, slots, wells, the hollow of a box or rim) outside the union() of the solids; a cutter that opens through a face runs 0.5 mm past it, and cutters that meet overlap by 0.5 mm; features that stand inside a cut-out (ribs in a cavity, pins in a pocket) are unioned after that cut; one connected piece; sizes exactly as in the spec; no include, use, import or surface.{feedback}"""

ART_ASK = """Write the prompts for this ONE artistic part, from its brief:

{brief}

Rules: one part, one design, no alternatives. The image prompt is for a picture the owner checks before any 3D work: one object, plain light background, three-quarter view, showing its shape and scale. The Meshy prompt makes the 3D model: style, size in mm ({env}), flat bottom for printing, no thin spikes under 1.5 mm.{feedback}

Answer with JSON only: {{"image_prompt": "...", "meshy_prompt": "..."}}"""

MANUAL_ASK = """Write the build manual for "{name}", for someone who has a 3D printer but has never made it.
{what}
Its parts, from their approved briefs and build specs:

{parts}

Write Markdown with these sections, in this order:
## What you need
A table: every printed part (count, material, about how long it prints if you can tell), every bought part (magnets with their exact size, glue, screws), and the tools.
## Print settings
For each printed part: its print orientation (from its brief), supports or none, and anything to watch (first layer, bridging).
## Build steps
Numbered steps, one action each, in the order you'd really do them. Each step: what to do, which parts, and a "Check:" line saying how to tell it's right (fit, polarity, flush). Magnets: test the polarity before gluing.
## Finishing
Cleanup, painting if it applies.
## Safety
Small parts and magnets are a swallowing hazard: keep them away from children under 3 and pets. Glue fumes: ventilate.

Only what's in the parts above: don't invent features or sizes."""

SYSTEM = ("You design 3D-printed parts for tabletop gaming. You answer exactly what is asked, about exactly one "
          "part, in the format asked. When asked for JSON, answer with the JSON only.")


def bind(fleet):
    """Use this fleet module (its queue, policy and state folder)."""
    global F
    F = fleet


def ensure(c):
    c.execute("""CREATE TABLE IF NOT EXISTS designs (
        id INTEGER PRIMARY KEY, name TEXT, route TEXT DEFAULT '', product TEXT DEFAULT '',
        parent_id INTEGER DEFAULT 0, source INTEGER DEFAULT 0, focus TEXT DEFAULT '', purpose TEXT DEFAULT '',
        stage TEXT, task_id INTEGER DEFAULT 0, brief TEXT DEFAULT '', spec TEXT DEFAULT '',
        model_id TEXT DEFAULT '', size TEXT DEFAULT '', tries INTEGER DEFAULT 0, feedback TEXT DEFAULT '',
        note TEXT DEFAULT '', by TEXT DEFAULT '', created TEXT, updated TEXT)""")
    have = {r["name"] for r in c.execute("PRAGMA table_info(designs)")}
    for col, decl in (("manual", "TEXT DEFAULT ''"), ("manual_task", "INTEGER DEFAULT 0"), ("scad", "TEXT DEFAULT ''")):
        if col not in have:
            c.execute(f"ALTER TABLE designs ADD COLUMN {col} {decl}")


def folder(did):
    d = F.STATE / "designs" / str(int(did))
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dir():
    return Path(os.path.expanduser((F.policy().get("design") or {}).get("output_dir") or OUTPUT))


def _slug(name):
    """A folder or file name anyone can open: letters, digits, spaces, . _ -"""
    s = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ._-]+", "", name or "")).strip(" .")
    return s[:60].rstrip(" .") or "part"


def saved_path(c, d):
    """The kept folder of a design: idea-<task>/ for the parts list, then
    <product>/ for a product and <product>/<part - id>/ for its parts; a
    single-part design is idea-<task>/<part - id>/."""
    base = output_dir() / f"idea-{d['source']}"
    if d["stage"] in ("parts", "listed"):
        return base
    if d["parent_id"]:
        p = c.execute("SELECT name FROM designs WHERE id=?", (d["parent_id"],)).fetchone()
        return base / _slug(p["name"] if p else d["product"]) / f"{_slug(d['name'])} - {d['id']}"
    if d["product"] and not d["route"]:              # a product's assembly row
        return base / _slug(d["name"])
    return base / f"{_slug(d['name'])} - {d['id']}"


def _show(path):
    """A path as the owner would type it (~ for the home folder)."""
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


def save(c, d):
    """Copy an approved part's files from the working folder to its kept folder."""
    src, dest = folder(d["id"]), saved_path(c, d)
    dest.mkdir(parents=True, exist_ok=True)
    for name, out in (("part.stl", _slug(d["name"]) + ".stl"), ("render.png", "preview.png")):
        if (src / name).exists():
            shutil.copyfile(src / name, dest / out)
    (dest / "spec.txt").write_text((d["spec"] or "").strip() + "\n")
    if d["brief"]:
        (dest / "brief.json").write_text(json.dumps(json.loads(d["brief"]), indent=1) + "\n")
    size = " × ".join(f"{x:g}" for x in json.loads(d["size"])) + " mm" if d["size"] else "not built"
    (dest / "about.txt").write_text(
        f"{d['name']}\n" + (f"Part of: {d['product']}\n" if d["product"] else "")
        + f"Purpose: {d['purpose'] or '-'}\nSize as built (X × Y × Z, Z up as printed): {size}\n"
        f"Design D{d['id']} from fleet task #{d['source']}; {d['note'] or ''} ({d['updated']})\n")
    return dest


def zip_of(did):
    """(file name, zip bytes) of a design's kept folder (a part, a product or a
    whole idea), or None while nothing is kept there yet."""
    with F.conn() as c:
        d = _row(c, did)
        dest = saved_path(c, d) if d else None
    files = sorted(f for f in dest.rglob("*") if f.is_file()) if dest and dest.is_dir() else []
    if not files:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, str(Path(dest.name) / f.relative_to(dest)))
    return dest.name + ".zip", buf.getvalue()


def bed():
    return list((F.policy().get("design") or {}).get("bed_mm") or BED)


def spec_chars():
    return int((F.policy().get("design") or {}).get("max_spec_chars") or SPEC_CHARS)


def image_url():
    return ((F.policy().get("tools") or {}).get("image") or {}).get("url") or "http://127.0.0.1:8963"


IMAGE_RETRY_S = 300          # a busy image service (GPUs in use) is asked again after this
_image_next = {}             # design id -> when to ask the image service again


def picture(c, d):
    """An artistic part's picture from its image prompt (image/image.py, FLUX.1 schnell),
    for the owner's look before any Meshy work. Busy GPUs: try again later."""
    if time.time() < _image_next.get(d["id"], 0):
        return 0
    try:
        prompts = parse_json(d["spec"])
        prompt = str(prompts.get("image_prompt") or "").strip()
        if not prompt:
            raise ValueError("no image_prompt")
    except ValueError as e:
        _set(c, d["id"], stage="prompt", task_id=0, tries=d["tries"] + 1, spec="",
             feedback=f"answer with the JSON asked for ({e})", note="prompts unreadable; asking again")
        return 1
    req = urllib.request.Request(image_url().rstrip("/") + "/image",
                                 json.dumps({"prompt": prompt[:1500], "seed": int(d["id"]), "width": 768,
                                             "height": 768}).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            png = r.read()
    except urllib.error.HTTPError as e:
        why = e.read().decode(errors="replace")[:200]
        _image_next[d["id"]] = time.time() + IMAGE_RETRY_S
        if e.code == 503:
            _set(c, d["id"], note=f"picture waits: {why}")
            return 0
        _set(c, d["id"], note=f"the image service refused it: {why}")
        return 0
    except OSError as e:
        _image_next[d["id"]] = time.time() + IMAGE_RETRY_S
        _set(c, d["id"], note=f"the image service isn't running ({str(e)[:80]}): image/install.sh")
        return 0
    out = folder(d["id"])
    (out / "render.png").write_bytes(png)
    (out / "picture.png").write_bytes(png)
    _image_next.pop(d["id"], None)
    _set(c, d["id"], stage="look", note="picture from its image prompt (FLUX.1 schnell); waiting for your look. "
         "Approve keeps it for the Meshy step; Change rewrites the prompts")
    return 1


def print3d_url():
    return ((F.policy().get("tools") or {}).get("print3d") or {}).get("url") or "http://127.0.0.1:8000"


def _set(c, did, **kw):
    kw["updated"] = F.now()
    c.execute(f"UPDATE designs SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), did))


def _row(c, did):
    ensure(c)
    return c.execute("SELECT * FROM designs WHERE id=?", (did,)).fetchone()


# ---------------- starting ----------------

def start(source, focus="", by=""):
    """Design from a finished task's answer (#301 and the like): a local task lists
    the candidate parts, then each becomes its own design. Returns the design id."""
    with F.conn() as c:
        ensure(c)
        t = c.execute("SELECT status, result FROM tasks WHERE id=?", (int(source),)).fetchone()
        if not t or t["status"] != "done" or not (t["result"] or "").strip():
            raise ValueError(f"#{source} has no finished answer to design from")
    ask = PARTS_ASK.format(focus=f" \"{focus}\"" if focus else "s", routes=ROUTE_NOTE)
    tid = F.submit_followup([int(source)], ask, kind="design-parts")
    with F.conn() as c:
        cur = c.execute("INSERT INTO designs (name, stage, task_id, source, focus, by, created, updated) "
                        "VALUES (?,?,?,?,?,?,?,?)", (f"parts of #{source}" + (f": {focus}" if focus else ""),
                                                     "parts", tid, int(source), focus, by, F.now(), F.now()))
        return cur.lastrowid


# ---------------- answers ----------------

def parse_json(text):
    """The first JSON object in an answer (models wrap it in ``` fences or add words)."""
    text = re.sub(r"```(?:json)?", "", text or "")
    start = text.find("{")
    while start >= 0:
        depth, end = 0, None
        for i, ch in enumerate(text[start:], start):
            depth += {"{": 1, "}": -1}.get(ch, 0)
            if depth == 0:
                end = i + 1
                break
        if end:
            try:
                return json.loads(text[start:end])
            except ValueError:
                pass
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in the answer")


def parts_from(answer):
    """[{name, route, product, purpose}] from a design-parts answer; bad entries dropped."""
    out = []
    for p in parse_json(answer).get("parts") or []:
        if not isinstance(p, dict) or not str(p.get("name", "")).strip():
            continue
        route = str(p.get("route", "")).lower().strip()
        out.append({"name": str(p["name"]).strip()[:120], "route": route if route in ROUTES else "structural",
                    "product": str(p.get("product") or "").strip()[:120],
                    "purpose": str(p.get("purpose") or "").strip()[:400]})
    if not out:
        raise ValueError("the answer listed no parts")
    return out


def check_brief(answer, bed_mm=None):
    """The brief as a dict, or ValueError saying what's wrong with it (one part, a size, fits the bed)."""
    b = parse_json(answer)
    if isinstance(b.get("parts"), list) or isinstance(b.get("options"), list) or isinstance(b.get("variants"), list):
        raise ValueError("the brief lists several parts or options; it must be one part")
    env = b.get("envelope_mm")
    if not (isinstance(env, list) and len(env) == 3 and all(isinstance(x, (int, float)) and x > 0 for x in env)):
        raise ValueError("envelope_mm must be three sizes in mm, [x, y, z]")
    if not fits_bed(env, bed_mm or BED):
        raise ValueError(f"envelope {env} mm doesn't fit the bed {bed_mm or BED} mm; split it into parts")
    big = oversized(b.get("features"), env)
    if big:
        raise ValueError(f"a feature is {big}, bigger than the envelope {env} mm: make envelope_mm the box around "
                         "the features")
    return b


def oversized(features, env):
    """A feature's own size (not a spacing or a fit) bigger than the envelope's largest side, or ''.
    Catches a brief whose envelope wasn't worked out from its features (2026-10-29)."""
    top = max(env) + max(1.0, 0.03 * max(env))
    for f in features or []:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*mm\b", str(f)):
            before = str(f)[max(0, m.start() - 30):m.start()].lower()
            if float(m.group(1)) > top and not re.search(r"spac|apart|interval|pitch|from|fits?|rang|up to|total", before):
                return f"{m.group(1)} mm ({str(f)[:60]})"
    return ""


SCAD_BLOCK = re.compile(r"```(?:openscad|scad)[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def spec_and_code(answer):
    """(the spec text, the OpenSCAD from its ```openscad block or ''). Owner 2026-10-31: the
    spec writer (Gemini) writes the code too; the local model turned correct specs into wrong CSG."""
    blocks = SCAD_BLOCK.findall(answer or "")
    spec = SCAD_BLOCK.sub("", answer or "").strip()
    return spec, (blocks[-1].strip() if blocks else "")


CONFLICT = re.compile(r"(?is)^\W*conflict\s*:\s*(.+)")
PAPERED = re.compile(r"(?i)\b(adjusted|changed|increased|enlarged|reduced|widened)\b[^.\n]{0,60}\b(match|fit|meet)\w*\b[^.\n]{0,30}\benvelope"
                     r"|\bbrief (?:specified|says|said|states)\b[^.\n]{0,80}\bbut\b")


def brief_conflict(spec):
    """Why a spec says its brief contradicts itself (it answered CONFLICT:, or it quietly
    changed a size to make the envelope fit, as #5's did), or ''."""
    m = CONFLICT.match(spec or "")
    if m:
        return m.group(1).strip()[:300]
    m = PAPERED.search(spec or "")
    return f"the spec had to change the brief to make it fit: \"{m.group(0)[:160]}\"" if m else ""


def fits_bed(env, bed_mm):
    """Fits lying any way round on the bed (the two flat sizes either way; height as given)."""
    x, y, z = env
    bx, by_, bz = bed_mm
    return z <= bz and ((x <= bx and y <= by_) or (y <= bx and x <= by_))


def multi_design(text):
    """Why a spec reads as more than one design, or ''."""
    t = text or ""
    if re.search(r"(?im)^\s*(#+\s*)?(option|variant|alternative)\s*[A-Z0-9]\b", t):
        return "it offers options or variants"
    heads = re.findall(r"(?im)^\W{0,6}(?:part|design)\s+(?:\d+|[A-H])\b", t)
    if len(heads) > 1:                  # "### 1. Base plate" is a feature of one part, not another part
        return f"it describes {len(heads)} separate parts"
    return ""


# ---------------- STL size ----------------

def stl_size(data):
    """[x, y, z] extent of an STL (binary or ASCII), in its units (mm)."""
    lo, hi = [float("inf")] * 3, [float("-inf")] * 3

    def take(v):
        for i in range(3):
            lo[i], hi[i] = min(lo[i], v[i]), max(hi[i], v[i])
    n = struct.unpack("<I", data[80:84])[0] if len(data) >= 84 else -1
    if n >= 0 and 84 + 50 * n == len(data):      # binary (its size gives it away; some start with "solid" too)
        for k in range(n):
            o = 84 + 50 * k + 12
            for j in range(3):
                take(struct.unpack("<3f", data[o + 12 * j:o + 12 * j + 12]))
    else:
        for m in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data):
            take([float(x) for x in m.groups()])
    if lo[0] == float("inf"):
        raise ValueError("the STL has no triangles")
    return [round(hi[i] - lo[i], 2) for i in range(3)]


def size_ok(got, want):
    """'' when the built size matches the brief's envelope (each size within 1 mm or 3%,
    the two flat sizes either way round), else what's wrong."""
    def near(a, b):
        return abs(a - b) <= max(1.0, 0.03 * b)
    flat = (near(got[0], want[0]) and near(got[1], want[1])) or (near(got[0], want[1]) and near(got[1], want[0]))
    if flat and near(got[2], want[2]):
        return ""
    return (f"it was built {got[0]:g} × {got[1]:g} × {got[2]:g} mm, but the brief says "
            f"{want[0]:g} × {want[1]:g} × {want[2]:g} mm (X × Y × Z, Z up as printed)")


# ---------------- the 3D print tool ----------------

def _call(path, body=None, timeout=60, raw=False):
    req = urllib.request.Request(print3d_url().rstrip("/") + path,
                                 json.dumps(body).encode() if body is not None else None,
                                 {"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if raw else json.loads(data)


def _thumbnail(stl, size=512, view="iso"):
    """A PNG preview of an STL from the 3D print tool's /thumbnail (multipart upload).
    view: iso (three-quarter), top, bottom or front (3D print tool #31)."""
    edge = uuid.uuid4().hex
    extra = f"--{edge}\r\nContent-Disposition: form-data; name=\"view\"\r\n\r\n{view}\r\n" if view != "iso" else ""
    body = (extra + f"--{edge}\r\nContent-Disposition: form-data; name=\"size\"\r\n\r\n{size}\r\n"
            f"--{edge}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"part.stl\"\r\n"
            f"Content-Type: model/stl\r\n\r\n").encode() + stl + f"\r\n--{edge}--\r\n".encode()
    req = urllib.request.Request(print3d_url().rstrip("/") + "/thumbnail", body,
                                 {"Content-Type": f"multipart/form-data; boundary={edge}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


SHAPE_ASK = """These are renders of ONE 3D-printed part, plain grey: picture 1 is a three-quarter view, picture 2 is from straight above, picture 3 from straight below. Its brief:

{brief}

For each feature in the brief, say whether you can see it in the pictures. Features under about 2 mm (engraving depth, small chamfers, clearances) can't be judged from renders: mark them "unclear", not "missing". Then list anything clearly wrong with the part as a whole: pieces floating apart, closed where it must be open, open where it must be closed, a different kind of object.

Answer with JSON only:
{{"features": [{{"feature": "...", "seen": "yes" or "missing" or "unclear"}}], "wrong": ["..."]}}"""


def vision_endpoint():
    """A machine with the vision model already loaded (policy design.vision_model,
    default qwen3.6:27b), or None: the check never loads a model, so it never
    pushes another job's model out of a GPU."""
    want = (F.policy().get("design") or {}).get("vision_model") or "qwen3.6:27b"
    for ep in F.endpoints(fresh=True):
        if ep.get("up") and any(m == want or m.split(":latest")[0] == want for m in ep.get("loaded") or []):
            return dict(ep, model=want)
    return None


def shape_check(brief, pictures):
    """('', note) when the renders show the brief's features, (what's missing, note)
    when they clearly don't, ('', 'skipped: ...') when no vision model is free."""
    ep = vision_endpoint()
    if ep is None:
        return "", "shape check skipped: no machine has the vision model loaded"
    try:
        answer = F.ollama(ep, SYSTEM, SHAPE_ASK.format(brief=json.dumps(brief, indent=1)), json_out=True,
                          timeout=600, images=[base64.b64encode(p).decode() for p in pictures])
        v = parse_json(answer)
    except (OSError, ValueError) as e:
        return "", f"shape check skipped: {str(e)[:120]}"
    feats = [f for f in v.get("features") or [] if isinstance(f, dict)]
    missing = [str(f.get("feature", ""))[:120] for f in feats if str(f.get("seen", "")).lower() == "missing"]
    wrong = [str(w)[:160] for w in v.get("wrong") or [] if str(w).strip()]
    seen = sum(str(f.get("seen", "")).lower() == "yes" for f in feats)
    problems = "; ".join(([f"missing: {', '.join(missing)}"] if missing else []) + wrong)
    return problems, (f"shape check: {seen} of {len(feats)} features seen" + (f"; {problems}" if problems else ""))


TOOL_RETRY_S = 120             # a 3D print tool that isn't answering is tried again after this
_tool_next = [0.0]


def _tool_down(e, detail):
    """The 3D print tool (or the Ollama behind it) is down or restarting: not the design's
    fault, so no try is used (2026-10-28: a redeploy cost 39 designs a try each)."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (502, 503, 504) or "Ollama request failed" in detail
    return isinstance(getattr(e, "reason", e), (ConnectionRefusedError, ConnectionResetError))


def build(c, d):
    """Build a structural part with OpenSCAD, check its size, render it. Moves the
    design to `look`, or back to `prompt` with the numbers when the size is off."""
    if time.time() < _tool_next[0]:
        return
    brief = json.loads(d["brief"])
    try:
        # use_templates False: a spec mentioning a "base plate" isn't the tool's stock plate (2026-10-28)
        body = {"prompt": d["spec"], "backend": "openscad", "use_templates": False}
        if d["scad"]:
            body["scad"] = d["scad"]          # rendered as written (3D print tool #34), no local LLM
        made = _call("/generate", body, timeout=900)
        stl = _call(f"/models/{made['model_id']}/download", raw=True, timeout=120)
        got = stl_size(stl)
    except (OSError, ValueError, KeyError) as e:
        detail = e.read().decode(errors="replace")[:300] if isinstance(e, urllib.error.HTTPError) else str(e)[:300]
        if _tool_down(e, detail):
            _tool_next[0] = time.time() + TOOL_RETRY_S
            return _set(c, d["id"], note=f"the 3D print tool isn't answering ({detail[:120]}); the build waits, no try used")
        return _retry(c, d, f"the build failed: {detail}")
    out = folder(d["id"])
    (out / "part.stl").write_bytes(stl)
    (out / "part.scad.txt").write_text(d["spec"])
    if d["scad"]:
        (out / "part.scad").write_text(d["scad"])
    wrong = size_ok(got, brief["envelope_mm"])
    _set(c, d["id"], model_id=made["model_id"], size=json.dumps(got))
    if wrong and d["tries"] + 1 < MAX_TRIES:
        return _retry(c, d, wrong)
    pictures, picture = {}, ""
    for view, name in (("iso", "render.png"), ("top", "render-top.png"), ("bottom", "render-bottom.png")):
        try:
            pictures[view] = _thumbnail(stl, view=view)
            (out / name).write_bytes(pictures[view])
        except (OSError, ValueError) as e:
            picture = f"; no {view} preview ({str(e)[:100]})"
    shape = ""
    if not wrong and len(pictures) == 3:
        problems, shape = shape_check(brief, [pictures["iso"], pictures["top"], pictures["bottom"]])
        if problems and d["tries"] + 1 < MAX_TRIES:
            return _retry(c, d, "the shape check of the renders found: " + problems)
    _set(c, d["id"], stage="look", note=("SIZE STILL OFF after %d builds: %s" % (d["tries"] + 1, wrong) if wrong
                                         else f"built {got[0]:g} × {got[1]:g} × {got[2]:g} mm, matches the brief")
         + (f"; {shape}" if shape else "") + picture + "; waiting for your look")


def _retry(c, d, why):
    tries = d["tries"] + 1
    if tries >= MAX_TRIES:
        _set(c, d["id"], stage="failed", tries=tries, note=f"gave up after {tries} tries: {why}")
        return
    _set(c, d["id"], stage="prompt", tries=tries, task_id=0, feedback=why, note=f"try {tries + 1}: {why}")


# ---------------- the loop ----------------

def _task(c, tid):
    return c.execute("SELECT status, result FROM tasks WHERE id=?", (tid,)).fetchone() if tid else None


def _ask(d, ask, kind):
    """A thinking stage. Parts and briefs build on the idea's source task (its answer is
    the context); a spec needs only its brief, which keeps it inside a paid AI's prompt
    cap. A kind whose policy says frontier_auto may use paid AI without asking."""
    auto = bool(F.kinds().get(kind, {}).get("frontier_auto"))
    if kind == "design-prompt":
        return F.submit(kind, ask, None, False, auto, "")
    return F.submit_followup([d["source"]], ask, kind=kind, frontier=auto)


def step():
    """Advance designs one stage each where they can go. Returns how many moved."""
    moved = 0
    with F.conn() as c:
        ensure(c)
        rows = c.execute("SELECT * FROM designs WHERE stage NOT IN ('approved','listed','failed','cancelled','look')"
                         " ORDER BY id").fetchall()
    for d in rows:
        with F.conn() as c:
            d = _row(c, d["id"])
            if d is None or d["stage"] in FINAL:
                continue
            moved += _advance(c, d) or 0
    with F.conn() as c:
        for d in c.execute("SELECT * FROM designs WHERE manual IN ('wanted', 'writing') ORDER BY id").fetchall():
            moved += _manual_step(c, d)
    return moved


def _advance(c, d):
    t = _task(c, d["task_id"])
    if d["stage"] in ("parts", "brief") or (d["stage"] == "prompt" and d["task_id"]):
        if t is None:
            if d["stage"] == "brief":
                return _start_brief(c, d)
            return 0
        if t["status"] in ("failed", "cancelled", "denied", "expired", "blocked"):
            _set(c, d["id"], stage="failed", note=f"its task #{d['task_id']} ended {t['status']}")
            return 1
        if t["status"] != "done":
            return 0
    if d["stage"] == "parts":
        try:
            parts = parts_from(t["result"])
        except ValueError as e:
            _set(c, d["id"], stage="failed", note=f"#{d['task_id']}: {e}")
            return 1
        products = {}
        for p in parts:
            if p["product"] and sum(q["product"] == p["product"] for q in parts) > 1 and p["product"] not in products:
                products[p["product"]] = c.execute(
                    "INSERT INTO designs (name, stage, product, source, by, note, created, updated) VALUES "
                    "(?,?,?,?,?,?,?,?)", (p["product"], "assembly", p["product"], d["source"], d["by"],
                                          "waits until each of its parts is approved", F.now(), F.now())).lastrowid
            c.execute("INSERT INTO designs (name, route, product, parent_id, source, purpose, stage, by, created, updated)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?)", (p["name"], p["route"], p["product"],
                                                        products.get(p["product"], 0), d["source"], p["purpose"],
                                                        "brief", d["by"], F.now(), F.now()))
        _set(c, d["id"], stage="listed", note=f"{len(parts)} parts" + (f", {len(products)} products to assemble"
                                                                         if products else ""))
        return 1
    if d["stage"] == "brief":
        try:
            b = check_brief(t["result"], bed())
        except ValueError as e:
            return _retry_brief(c, d, str(e))
        b["route"] = d["route"] or b.get("route", "structural")
        _set(c, d["id"], brief=json.dumps(b), stage="prompt", task_id=0, tries=0, feedback="",
             note="brief done: " + " × ".join(f"{x:g}" for x in b["envelope_mm"]) + " mm")
        return 1
    if d["stage"] == "prompt" and not d["task_id"]:
        b = json.loads(d["brief"])
        fb = ""
        if d["feedback"]:
            fb = f"\n\nChange from the last version: {d['feedback']}."
            if d["spec"]:
                fb += f" The last version is below; fix that, keep the rest:\n\n{d['spec'][:spec_chars()]}"
                if d["scad"]:
                    fb += f"\n\n```openscad\n{d['scad'][:2 * spec_chars()]}\n```"
        env = " × ".join(f"{x:g}" for x in b["envelope_mm"]) + " mm"
        ask = (SPEC_ASK if d["route"] != "artistic" else ART_ASK).format(brief=json.dumps(b, indent=1), env=env,
                                                                       feedback=fb, limit=spec_chars())
        _set(c, d["id"], task_id=_ask(d, ask, "design-prompt"), note="writing the " +
             ("build spec" if d["route"] != "artistic" else "image and Meshy prompts"))
        return 1
    if d["stage"] == "prompt":
        spec, code = (t["result"] or "").strip(), ""
        if d["route"] != "artistic":
            spec, code = spec_and_code(spec)
        conflict = brief_conflict(spec) if d["route"] != "artistic" else ""
        if conflict:
            return _retry_brief(c, d, f"the brief contradicts itself: {conflict}", restart=True)
        many = multi_design(spec)
        if not many and d["route"] != "artistic" and len(spec) > spec_chars():
            many = (f"it's {len(spec)} characters and the build tool takes at most {spec_chars()}: cut the "
                    "explanations, keep every size and position")
            _set(c, d["id"], spec=spec)          # the rewrite starts from it
        if many:
            _set(c, d["id"], task_id=0, tries=d["tries"] + 1,
                 feedback=many if "characters" in many else f"{many}; write exactly one design",
                 note=f"spec rejected: {many}")
            if d["tries"] + 1 >= MAX_TRIES:
                _set(c, d["id"], stage="failed", note=f"spec rejected {d['tries'] + 1} times: {many}")
            return 1
        if d["route"] == "artistic":
            _set(c, d["id"], spec=spec, stage="image", task_id=0, note="prompts written; making the picture")
            return 1
        _set(c, d["id"], spec=spec, scad=code, stage="build", task_id=0,
             note="building its OpenSCAD" if code else "building in OpenSCAD (the 3D print tool writes the code)")
        return 1
    if d["stage"] == "build":
        build(c, d)
        return 1
    if d["stage"] == "image":
        return picture(c, d)
    if d["stage"] == "assembly":
        kids = c.execute("SELECT stage FROM designs WHERE parent_id=?", (d["id"],)).fetchall()
        done = sum(k["stage"] == "approved" for k in kids)
        note = f"{done} of {len(kids)} parts approved; the assembly step comes once all are"
        if note != d["note"]:
            _set(c, d["id"], note=note)
        return 0
    return 0


def _start_brief(c, d):
    sib = c.execute("SELECT name FROM designs WHERE parent_id=? AND parent_id != 0 AND id != ?",
                    (d["parent_id"], d["id"])).fetchall() if d["parent_id"] else []
    product = (f"It's one part of the product \"{d['product']}\"; the other parts ({', '.join(s['name'] for s in sib)})"
               " have their own briefs: give the sizes where it meets them." if sib else "")
    fb = f"\nThe last brief was rejected: {d['feedback']}" if d["feedback"] else ""
    ask = BRIEF_ASK.format(name=d["name"], route=d["route"] or "structural", purpose=d["purpose"] or "(see the idea)",
                           product=product + fb, bed=" × ".join(f"{x:g}" for x in bed()))
    _set(c, d["id"], task_id=_ask(d, ask, "design-brief"), note="writing the brief")
    return 1


def _retry_brief(c, d, why, restart=False):
    """The brief again, with why. restart: from a later stage (its spec found the brief contradicts itself)."""
    tries = d["tries"] + 1
    if tries >= MAX_TRIES:
        _set(c, d["id"], stage="failed", tries=tries, note=f"brief rejected {tries} times: {why}")
    else:
        _set(c, d["id"], stage="brief", task_id=0, tries=tries, feedback=why, note=f"brief rejected: {why}",
             **({"spec": ""} if restart else {}))
    return 1


def rebrief_candidates():
    """Structural designs waiting for the owner's look, or failed: what 'rebrief' with no id redoes."""
    with F.conn() as c:
        ensure(c)
        return [r["id"] for r in c.execute("SELECT id FROM designs WHERE stage IN ('look', 'failed') AND "
                                           "COALESCE(route, '') != 'artistic' ORDER BY id")]


def rebrief(did, words="", by=""):
    """The owner sends a design back to a fresh brief (2026-10-29: briefs written by Gemini
    after local ones contradicted themselves). Its spec and build start again from it."""
    with F.conn() as c:
        d = _row(c, did)
        if not d:
            raise ValueError(f"no design {did}")
        if d["stage"] in ("assembly", "listed", "cancelled") or d["route"] == "artistic":
            raise ValueError(f"design {did} ({d['stage']}, {d['route'] or 'structural'}) can't be re-briefed")
        _set(c, did, stage="brief", task_id=0, tries=0, spec="", feedback=(words or "").strip(),
             note="brief being rewritten" + (f": {words.strip()}" if (words or "").strip() else ""))
    F.ledger({"event": "design-rebrief", "design": did, "by": by})


# ---------------- the owner ----------------

# ---------------- manuals (an option the owner picks) ----------------

def want_manual(did, by=""):
    """The owner asks for a build manual: for an approved part, or a product once
    all its parts are approved (it waits until then)."""
    with F.conn() as c:
        d = _row(c, did)
        if not d:
            raise ValueError(f"no design {did}")
        if d["stage"] == "assembly":
            pass
        elif d["stage"] != "approved":
            raise ValueError(f"design {did} isn't approved yet (it's at {d['stage']}); approve it first")
        if d["manual"] in ("wanted", "writing"):
            raise ValueError(f"design {did}'s manual is already on its way")
        _set(c, did, manual="wanted", manual_task=0)
    F.ledger({"event": "design-manual", "design": did, "by": by})


def _manual_parts(c, d):
    """The approved parts a manual covers: the design itself, or a product's parts."""
    if d["stage"] == "assembly":
        return c.execute("SELECT * FROM designs WHERE parent_id=? ORDER BY id", (d["id"],)).fetchall()
    return [d]


def _manual_step(c, d):
    parts = _manual_parts(c, d)
    if d["manual"] == "wanted":
        if not parts or any(p["stage"] != "approved" for p in parts):
            return 0                                   # a product's manual waits for all its parts
        blocks = []
        for p in parts:
            blocks.append(f"### {p['name']} ({p['route'] or 'structural'})\nBrief: {p['brief']}\n\nBuild spec:\n{p['spec']}"
                          + (f"\n\nAs built: {' × '.join(f'{x:g}' for x in json.loads(p['size']))} mm" if p["size"] else ""))
        what = (f"It's a product made of {len(parts)} printed parts that go together." if len(parts) > 1
                else "It's one printed part.")
        ask = MANUAL_ASK.format(name=d["name"], what=what, parts="\n\n".join(blocks))
        _set(c, d["id"], manual="writing", manual_task=F.submit_followup([d["source"]], ask, kind="design-manual"))
        return 1
    t = _task(c, d["manual_task"])
    if t is None or t["status"] not in ("done", "failed", "cancelled", "denied", "expired", "blocked"):
        return 0
    if t["status"] != "done":
        _set(c, d["id"], manual="failed")
        return 1
    def page(pics):
        return f"# {d['name']}: build manual\n\n" + "".join(f"![{n}]({p})\n" for n, p in pics) + "\n" + t["result"].strip() + "\n"
    pics = [(p["name"], f"../{p['id']}/render.png") for p in parts
            if (F.STATE / "designs" / str(p["id"]) / "render.png").exists()]
    (folder(d["id"]) / "manual.md").write_text(page(pics))
    dest = saved_path(c, d)                           # and next to the kept files, with their pictures
    dest.mkdir(parents=True, exist_ok=True)
    pics = [(p["name"], os.path.relpath(saved_path(c, p) / "preview.png", dest)) for p in parts
            if (saved_path(c, p) / "preview.png").exists()]
    (dest / "manual.md").write_text(page(pics))
    _set(c, d["id"], manual="done")
    return 1


def manual_text(did):
    path = F.STATE / "designs" / str(int(did)) / "manual.md"
    return path.read_text() if path.exists() else None


def approve(did, by=""):
    with F.conn() as c:
        d = _row(c, did)
        if not d or d["stage"] != "look":
            raise ValueError(f"design {did} isn't waiting for your look")
        _set(c, did, stage="approved", note=f"approved by {by or 'the owner'}")
        dest = save(c, _row(c, did))
    F.ledger({"event": "design-approve", "design": did, "by": by, "saved": _show(dest)})


def change(did, words, by=""):
    words = (words or "").strip()
    if not words:
        raise ValueError("say what to change")
    with F.conn() as c:
        d = _row(c, did)
        if not d or d["stage"] not in ("look", "approved", "failed"):
            raise ValueError(f"design {did} can't be changed now (it's at {d['stage'] if d else 'nothing'})")
        if not d["brief"]:
            raise ValueError(f"design {did} has no brief yet")
        _set(c, did, stage="prompt", task_id=0, tries=0, feedback=words[:2000], note=f"changing: {words[:120]}")
    F.ledger({"event": "design-change", "design": did, "by": by})


def cancel(did, by=""):
    with F.conn() as c:
        d = _row(c, did)
        if not d or d["stage"] in FINAL:
            return False
        _set(c, did, stage="cancelled", note=f"cancelled by {by or 'the owner'}")
        return True


def listing(limit=200):
    with F.conn() as c:
        ensure(c)
        rows = [dict(r) for r in c.execute("SELECT id, name, route, product, parent_id, source, stage, task_id, "
                                           "size, tries, note, manual, updated FROM designs ORDER BY id DESC LIMIT ?",
                                           (limit,))]
        for r in rows:
            r["render"] = (F.STATE / "designs" / str(r["id"]) / "render.png").exists()
            _where(c, r)
    return rows


def _where(c, r):
    """Adds the working folder, and the kept folder once anything is kept there."""
    r["work"] = _show(F.STATE / "designs" / str(r["id"]))
    dest = saved_path(c, r)
    r["saved"] = _show(dest) if dest.is_dir() and any(dest.iterdir()) else ""


def detail(did):
    with F.conn() as c:
        d = _row(c, did)
        if not d:
            return None
        out = dict(d)
        _where(c, out)
    out["render"] = (F.STATE / "designs" / str(did) / "render.png").exists()
    return out
