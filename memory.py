"""memory.py -- the fleet's shared memory: notes with search (board WP-152 step 2).

One SQLite file of notes (task results, decisions, things a session wants
the next one to know). A note's vector comes from a local embedding model
(fleet.py picks the machine and calls Ollama); a note stored while no
machine was free has no vector yet and is filled in later by the workers.
Search ranks notes with a vector by cosine similarity to the query's vector,
and notes without one (or every note, when no machine can embed the query)
by how many of the query's words they contain.

Standard library only. fleet.py loads this file and does the embedding;
nothing here calls a model.
"""
import math
import re
import sqlite3
from array import array
from datetime import datetime

WORD = re.compile(r"[a-z0-9][a-z0-9_.-]{2,}")


def now():
    return datetime.now().isoformat(timespec="seconds")


def open_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY, text TEXT NOT NULL, source TEXT DEFAULT '', tags TEXT DEFAULT '',
        task_id INTEGER DEFAULT 0, created TEXT, vec BLOB, model TEXT DEFAULT '')""")
    return c


def pack(vec):
    return array("f", vec).tobytes()


def unpack(blob):
    a = array("f")
    a.frombytes(blob)
    return a


def cosine(a, b):
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def clean_tags(tags):
    """Tags as stored: lowercased, no spaces or commas, joined with commas."""
    out = []
    for t in tags or ():
        t = re.sub(r"[^a-z0-9_.-]", "-", str(t).strip().lower()).strip("-")
        if t and t not in out:
            out.append(t)
    return ",".join(out)


def add(c, text, source="", tags=(), task_id=0, vec=None, model=""):
    cur = c.execute("INSERT INTO notes (text, source, tags, task_id, created, vec, model) VALUES (?,?,?,?,?,?,?)",
                    (text, source, clean_tags(tags), int(task_id or 0), now(), pack(vec) if vec else None,
                     model if vec else ""))
    return cur.lastrowid


def set_vec(c, nid, vec, model):
    c.execute("UPDATE notes SET vec=?, model=? WHERE id=?", (pack(vec), model, nid))


def missing(c, limit=32):
    """Notes still without a vector, oldest first."""
    return c.execute("SELECT id, text FROM notes WHERE vec IS NULL ORDER BY id LIMIT ?", (limit,)).fetchall()


def delete(c, nid):
    return c.execute("DELETE FROM notes WHERE id=?", (nid,)).rowcount > 0


def latest(c, n=20):
    return [_out(r) for r in c.execute("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (n,))]


def _out(r, score=None, how=""):
    d = {"id": r["id"], "text": r["text"], "source": r["source"], "created": r["created"],
         "tags": [t for t in r["tags"].split(",") if t], "task_id": r["task_id"], "has_vec": r["vec"] is not None}
    if score is not None:
        d["score"], d["how"] = round(score, 3), how
    return d


def words(text):
    return set(WORD.findall(text.lower()))


def search(c, query, qvec=None, model="", k=5, tags=()):
    """The k best notes for a query. With qvec (the query's vector from
    `model`), notes embedded by the same model are ranked by cosine
    similarity; every other note by the share of the query's words it has.
    A note with none of the query's words and no vector never shows up."""
    want = set(clean_tags(tags).split(",")) - {""}
    qwords = words(query)
    scored = []
    for r in c.execute("SELECT * FROM notes"):
        if want and not want <= set(r["tags"].split(",")):
            continue
        if qvec is not None and r["vec"] is not None and r["model"] == model:
            scored.append((cosine(qvec, unpack(r["vec"])), "meaning", r))
        elif qwords:
            hit = len(qwords & words(r["text"] + " " + r["source"] + " " + r["tags"].replace(",", " ")))
            if hit:
                scored.append((hit / len(qwords), "words", r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [_out(r, s, how) for s, how, r in scored[:max(1, k)]]


def count(c):
    r = c.execute("SELECT COUNT(*) AS n, SUM(vec IS NULL) AS waiting FROM notes").fetchone()
    return {"notes": r["n"], "waiting_for_vector": r["waiting"] or 0}
