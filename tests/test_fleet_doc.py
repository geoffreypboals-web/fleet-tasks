"""A task as a document (stacks/fleet-tasks/doc.py) and the live preview (fleet.py ollama streaming)."""
import importlib.util
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


doc = load("fleet_doc", HERE / "doc.py")

ANSWER = """PETG should be dried before printing.

## Drying
- Dry at **65 °C** for 4 hours ([Prusa guide](https://help.prusa3d.com/article/petg_2059)).
- Store it *sealed*.

| Filament | Temp |
|---|---|
| PETG | 65 °C |

1. Weigh the spool
2. Dry it

```
G28 ; home
```
See https://example.org/page for more."""

TASK = {"id": 7, "kind": "research", "status": "done", "status_words": "done", "by": "qwen3.6:27b@gpu-server-gpu01",
        "routed": "", "updated": "2026-10-26T22:00:00", "untrusted": 1, "result": ANSWER, "partial": "",
        "prompt": "How should I dry PETG?\n\nThe web pages to use are below.\n\n<untrusted source=\"x\">page</untrusted>",
        "notes": "[2026-10-26T21:59:00] read https://help.prusa3d.com/article/petg_2059: clean\n"}


def test_markdown_becomes_a_document():
    h = doc.to_html(ANSWER)
    assert "<h3>Drying</h3>" in h and "<strong>65 °C</strong>" in h and "<em>sealed</em>" in h
    assert '<a href="https://help.prusa3d.com/article/petg_2059" rel="noopener noreferrer nofollow"' in h
    assert "<table>" in h and "<th>Filament</th>" in h and "<ol><li>Weigh the spool</li>" in h
    assert "<pre>G28 ; home</pre>" in h and 'href="https://example.org/page"' in h


@pytest.mark.parametrize("evil", [
    "<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "[click](javascript:alert(1))",
    "[x](data:text/html,<script>alert(1)</script>)", '<a href="javascript:alert(1)">x</a>',
    "**<iframe src=//evil>**", "| <b onmouseover=alert(1)>a</b> |\n|---|\n| b |"])
def test_nothing_from_a_page_can_run(evil):
    h = doc.to_html(evil) + doc.page(dict(TASK, result=evil, prompt=evil))
    assert not re.search(r"<(script|img|iframe|b )", h)
    assert "<script" not in h and "<img" not in h and "<iframe" not in h and 'href="javascript' not in h
    assert 'href="data:' not in h


def test_the_page_shows_question_sources_and_warning_but_not_the_pages():
    p = doc.page(TASK)
    assert "<h1>How should I dry PETG?</h1>" in p and "untrusted source" not in p
    assert "Sources read" in p and "help.prusa3d.com/article/petg_2059" in p and "AI-defence screen" in p
    assert 'href="/doc/7.docx"' in p and "<script" not in p
    assert "script-src" not in doc.CSP and "default-src 'none'" in doc.CSP


def test_a_running_task_shows_the_answer_so_far():
    p = doc.page(dict(TASK, status="claimed", status_words="working", result="", partial="## Drying\nDry it"))
    assert "showing the answer so far" in p and "<h3>Drying</h3>" in p


def test_word_file_opens_and_has_the_content():
    data = doc.docx(TASK)
    z = zipfile.ZipFile(io.BytesIO(data))
    assert {"[Content_Types].xml", "word/document.xml", "word/styles.xml", "word/numbering.xml",
            "word/_rels/document.xml.rels", "_rels/.rels"} <= set(z.namelist())
    body = z.read("word/document.xml").decode()
    for part in ("How should I dry PETG?", "Drying", "65 °C", "Weigh the spool", "G28 ; home", "Sources read"):
        assert part in body
    assert 'w:val="Heading3"' in body and "<w:tbl>" in body and "<w:hyperlink" in body
    rels = z.read("word/_rels/document.xml.rels").decode()
    assert 'Target="https://help.prusa3d.com/article/petg_2059" TargetMode="External"' in rels
    import xml.dom.minidom
    for name in z.namelist():                                   # every part is well-formed XML
        xml.dom.minidom.parseString(z.read(name))


def test_word_file_escapes_markup_from_pages():
    body = zipfile.ZipFile(io.BytesIO(doc.docx(dict(TASK, result="a <w:p> & </w:body> b")))).read("word/document.xml")
    import xml.dom.minidom
    xml.dom.minidom.parseString(body)
    assert b"&lt;w:p&gt; &amp; &lt;/w:body&gt;" in body


def test_ask_of_drops_what_came_after_the_request():
    assert doc.ask_of(TASK["prompt"]) == "How should I dry PETG?"
    assert doc.ask_of("Review it\n\nThe code to work on is below (repo).\n\n### a.py") == "Review it"
    assert doc.ask_of("plain question") == "plain question"


# ---------------- the live preview ----------------

@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_POLICY", str(HERE / "tests" / "policy.json"))
    monkeypatch.setenv("FLEET_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("GPU_LEASE_DIR", str(tmp_path / "leases"))
    return load("fleet", HERE / "fleet.py")


class Stream(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_answer_streams_into_the_preview_and_is_cleared_when_done(fleet, monkeypatch):
    chunks = ["## Dry", "ing\n", "- 65 °C", " for 4 h"]
    lines = b"".join(json.dumps({"message": {"content": x}, "done": False}).encode() + b"\n" for x in chunks) + \
        json.dumps({"message": {"content": ""}, "done": True}).encode() + b"\n"
    seen = []
    monkeypatch.setattr(fleet, "PREVIEW_EVERY_S", 0)
    monkeypatch.setattr(fleet.urllib.request, "urlopen", lambda *a, **k: Stream(lines))
    ep = {"name": "x", "url": "http://x", "model": "qwen3.6:27b", "tier": "large"}
    assert fleet.ollama(ep, "", "q", on_text=seen.append) == "## Drying\n- 65 °C for 4 h"
    assert seen[-1] == "## Drying\n- 65 °C for 4 h" and len(seen) >= 4
    tid = fleet.submit("summarize", "x")
    c = fleet.db()
    task = fleet.claim(c, "large", "x")
    fleet.work_local(c, task, dict(ep, loaded=[]), [])
    t = c.execute("SELECT status, result, partial FROM tasks WHERE id=?", (tid,)).fetchone()
    assert t["status"] == "done" and t["partial"] == "" and t["result"].startswith("## Drying")


def test_an_answer_that_built_a_web_page_offers_it_sealed():
    ans = "Save this as guide.html:\n\n```html\n<!DOCTYPE html>\n<html><body><h1>SV08</h1><script>x()</script></body></html>\n```\nDone."
    assert doc.built_page(ans).startswith("<!DOCTYPE html>") and doc.built_page(ANSWER) is None
    assert 'href="/doc/7/page"' in doc.page(dict(TASK, result=ans))
    csp = doc.BUILT_CSP
    assert csp.startswith("sandbox allow-scripts") and "allow-same-origin" not in csp and "connect-src 'none'" in csp
    assert "form-action 'none'" in csp


def test_a_merged_jobs_question_is_just_the_request():
    merged = "Review StockOps\n\nThe text was too long for one machine, so it was split...\n\n--- Answer 1 ---\nlots"
    assert doc.ask_of(merged) == "Review StockOps"


def test_suggestions_come_from_the_answers_plan_headings():
    text = ("# Review\n## Findings\n- The site is slow\n## Prioritised improvements\n1. **Fix the banner:** it shows "
            "sample text\n2. Add [safety lines](https://x) to products\n### Why\nBecause.\n- still inside the plan\n"
            "## Summary\n- not a suggestion\n")
    assert doc.suggestions(text) == ["Fix the banner: it shows sample text", "Add safety lines to products",
                                     "still inside the plan"]
    assert doc.suggestions("Just prose, no plan.") == []
