"""doc.py -- a finished task as a document: a web page to read or print, and a Word file.

Answers are Markdown (headings, lists, tables, links, code). This turns
them into safe HTML (every character of the answer is escaped first; links
only for http and https; no images, no raw HTML), and into a .docx built
from the same blocks. A research answer carries text from web pages nobody
here wrote, so nothing in it can run on the page.

Standard library only.
"""
import html
import io
import re
import zipfile
from datetime import datetime

LINK = re.compile(r"\[([^\]\n]{1,300})\]\((https?://[^)\s]{1,2000})\)|(?<![\w/\"'=])(https?://[^\s<>)\]\"']{3,2000})")
INLINE = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_)")


# ---------------- Markdown to blocks ----------------

def blocks(text):
    """[(kind, data)]: ('h', (level, text)), ('p', text), ('ul'|'ol', [items]),
    ('code', text), ('quote', text), ('table', [rows]), ('hr', None)."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            body, i = [], i + 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            out.append(("code", "\n".join(body)))
            i += 1
            continue
        m = re.match(r"(#{1,6})\s+(.*)", s)
        if m:
            out.append(("h", (min(len(m.group(1)), 3), m.group(2).strip().rstrip("#").strip())))
            i += 1
            continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", s):
            out.append(("hr", None))
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.fullmatch(r"\|?\s*:?-{2,}.*", lines[i + 1].strip()):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.fullmatch(r"\|?[\s:|-]+\|?", lines[i].strip()):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append(("table", rows))
            continue
        if re.match(r"[-*+]\s+", s) or re.match(r"\d{1,3}[.)]\s+", s):
            kind = "ol" if re.match(r"\d", s) else "ul"
            items = []
            while i < len(lines) and lines[i].strip() and (re.match(r"\s*([-*+]|\d{1,3}[.)])\s+", lines[i]) or
                                                           (items and lines[i].startswith(("  ", "\t")))):
                t = lines[i].strip()
                m = re.match(r"([-*+]|\d{1,3}[.)])\s+(.*)", t)
                if m:
                    items.append(m.group(2))
                else:
                    items[-1] += " " + t
                i += 1
            out.append((kind, items))
            continue
        if s.startswith(">"):
            body = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                body.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(("quote", " ".join(body)))
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(r"(#{1,6}\s|```|[-*+]\s|\d{1,3}[.)]\s|>|\|)",
                                                                       lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        if not para:                     # a line no rule took: keep it as its own paragraph
            para, i = [s], i + 1
        out.append(("p", " ".join(para)))
    return out


def runs(text):
    """[(text, {'b','i','code'} flags, href or None)] for one line of inline Markdown."""
    out, pos = [], 0
    for m in LINK.finditer(text):
        out += _styled(text[pos:m.start()])
        label, href = (m.group(1), m.group(2)) if m.group(2) else (m.group(3), m.group(3))
        out.append((label, frozenset(), href))
        pos = m.end()
    return out + _styled(text[pos:])


def _styled(text):
    out, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], frozenset(), None))
        t = m.group(0)
        if t.startswith("`"):
            out.append((t[1:-1], frozenset({"code"}), None))
        elif t.startswith(("**", "__")):
            out.append((t[2:-2], frozenset({"b"}), None))
        else:
            out.append((t[1:-1], frozenset({"i"}), None))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], frozenset(), None))
    return out


# ---------------- HTML ----------------

TASK_LINK = re.compile(r"(?<![\w&;/=\"'#])#(\d{1,7})\b")


def _inline_html(text):
    parts = []
    for t, flags, href in runs(text):
        h = html.escape(t)
        if "code" not in flags and not href:      # "#238" is another fleet task: open it
            h = TASK_LINK.sub(r'<a href="/#task=\1" title="Open task #\1">#\1</a>', h)
        if "code" in flags:
            h = f"<code>{h}</code>"
        if "b" in flags:
            h = f"<strong>{h}</strong>"
        if "i" in flags:
            h = f"<em>{h}</em>"
        if href:
            h = f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer nofollow" target="_blank">{h}</a>'
        parts.append(h)
    return "".join(parts)


def to_html(text):
    out = []
    for kind, d in blocks(text):
        if kind == "h":
            out.append(f"<h{d[0] + 1}>{_inline_html(d[1])}</h{d[0] + 1}>")     # the page title is the h1
        elif kind == "p":
            out.append(f"<p>{_inline_html(d)}</p>")
        elif kind in ("ul", "ol"):
            out.append(f"<{kind}>" + "".join(f"<li>{_inline_html(x)}</li>" for x in d) + f"</{kind}>")
        elif kind == "code":
            out.append(f"<pre>{html.escape(d)}</pre>")
        elif kind == "quote":
            out.append(f"<blockquote>{_inline_html(d)}</blockquote>")
        elif kind == "table" and d:
            head = "".join(f"<th>{_inline_html(c)}</th>" for c in d[0])
            body = "".join("<tr>" + "".join(f"<td>{_inline_html(c)}</td>" for c in r) + "</tr>" for r in d[1:])
            out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
        elif kind == "hr":
            out.append("<hr>")
    return "\n".join(out)


PAGE_CSS = """
:root { --ink:#1d1d1f; --muted:#6b6b70; --line:#ddd; --paper:#fff; --bg:#f3f3f1; --link:#0b57d0 }
@media (prefers-color-scheme: dark) { :root { --ink:#e8e8e6; --muted:#a0a0a6; --line:#3a3a3e; --paper:#1f1f22; --bg:#141416; --link:#8ab4f8 } }
* { box-sizing: border-box }
body { margin:0; background:var(--bg); color:var(--ink); font: 17px/1.6 Georgia, "Times New Roman", serif }
.sheet { max-width: 820px; margin: 24px auto; background: var(--paper); padding: 48px 56px; border-radius: 6px;
         box-shadow: 0 1px 4px rgba(0,0,0,.12) }
@media (max-width: 700px) { .sheet { margin: 0; padding: 24px 16px; border-radius: 0 } }
h1 { font-size: 28px; line-height: 1.25; margin: 0 0 6px } h2 { font-size: 22px; margin: 28px 0 8px }
h3 { font-size: 18px; margin: 22px 0 6px } h4 { font-size: 16px; margin: 18px 0 4px }
.meta { color: var(--muted); font: 13px/1.5 system-ui, sans-serif; margin-bottom: 20px }
.ask { border-left: 3px solid var(--line); padding: 2px 0 2px 14px; color: var(--muted); font-style: italic; white-space: pre-wrap }
a { color: var(--link); overflow-wrap: anywhere } code, pre { font: 14px ui-monospace, Menlo, Consolas, monospace }
pre { white-space: pre-wrap; background: var(--bg); padding: 12px; border-radius: 4px }
table { border-collapse: collapse; width: 100%; font-size: 15px; margin: 12px 0; display: block; overflow-x: auto }
th, td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top }
blockquote { margin: 12px 0; padding-left: 14px; border-left: 3px solid var(--line); color: var(--muted) }
.sources li { font-size: 14px } .tools { font: 14px system-ui, sans-serif; display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px }
.tools a { padding: 6px 12px; border: 1px solid var(--line); border-radius: 6px; text-decoration: none }
.note { font: 13px system-ui, sans-serif; color: var(--muted); border-top: 1px solid var(--line); margin-top: 32px; padding-top: 10px }
@media print { body { background: #fff; color: #000 } .sheet { box-shadow: none; margin: 0; padding: 0; max-width: none }
               .tools { display: none } a { color: #000 } }
"""


ASK_ENDS = ("\n\nThe web pages to use are below.", "\n\n<untrusted", "\n\nThe code to work on is below",
            "\n\nThis is part ", "\n\nThe text was too long for one machine",
            "\n\nThis follows up an earlier task", "\n\nThe document to use is below.")


def ask_of(prompt):
    """The request itself, without the pages or code that were put after it."""
    cut = min([prompt.find(m) for m in ASK_ENDS if prompt.find(m) > 0] or [len(prompt)])
    return prompt[:cut].strip()


SUGGEST_HEADS = re.compile(r"improve|recommend|next step|action|plan|suggest|priorit|to.?do|follow|fix|change", re.I)


def suggestions(text, limit=20):
    """The list items under headings like "Improvements", "Next steps" or
    "Prioritised plan" (or a bold line like "**Improvement actions:**"):
    what the answer suggests doing, each as plain text, for the page's
    follow-up list. The document's own title (a first "# ..." line) doesn't
    count, or every list in it would. Empty when the answer has none."""
    out, inside, level, first = [], False, 0, True
    for line in (text or "").splitlines():
        h = re.match(r"\s*(#{1,6})\s+(.*)", line)
        b = None if h else re.match(r"\s*\*\*([^*]+)\*\*:?\s*$", line)
        if h or b:
            lvl, words = (len(h.group(1)), h.group(2)) if h else (7, b.group(1))
            title, first = first and lvl == 1, False
            if inside and lvl <= level:
                inside = False
            if not inside and not title and SUGGEST_HEADS.search(words):
                inside, level = True, lvl
            continue
        m = re.match(r"\s{0,3}(?:\d+[.)]|[-*+])\s+(.*)", line)
        if inside and m:
            item = re.sub(r"\*\*|__|`", "", m.group(1)).strip()
            item = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", item)     # links: keep the words
            if len(item) > 8 and item not in out:
                out.append(item[:400])
                if len(out) >= limit:
                    break
    return out


def title_for(task):
    first = (task.get("ask") or ask_of(task["prompt"])).strip().splitlines()[0] if (task.get("ask") or ask_of(task["prompt"])).strip() else ""
    first = re.sub(r"\s+", " ", first)
    return (first[:90] + "…") if len(first) > 90 else (first or f"Task {task['id']}")


def sources(task):
    """[(url, what the screen said)] from the task's notes ('read URL: ...')."""
    out = []
    for line in (task.get("notes") or "").splitlines():
        m = re.search(r"\] read (https?://\S+?)(?: \(from \S+\))?: (.*)$", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def page(task):
    """The whole document page for one finished (or running) task."""
    t = title_for(task)
    when = (task.get("updated") or "").replace("T", " ")[:16]
    meta = " · ".join(x for x in (f"#{task['id']} {task['kind']}", when, f"answered by {task['by']}" if task.get("by") else "",
                                    task.get("routed") or "") if x)
    answer = task.get("result") or task.get("partial") or ""
    status = "" if task.get("status") == "done" else \
        f'<p class="meta"><strong>{html.escape(task.get("status_words") or task.get("status", ""))}</strong>' \
        + (" — showing the answer so far" if task.get("partial") and not task.get("result") else "") + "</p>"
    built = f'<a href="/doc/{task["id"]}/page" target="_blank" rel="noopener">Open the web page it built</a>' \
        if built_page(answer) else ""
    src = sources(task)
    src_html = ""
    if src:
        src_html = "<h2>Sources read</h2><ol class=\"sources\">" + "".join(
            f'<li><a href="{html.escape(u, quote=True)}" rel="noopener noreferrer nofollow" target="_blank">'
            f"{html.escape(u)}</a> <span class=\"meta\">({html.escape(w)})</span></li>" for u, w in src) + "</ol>"
    untrusted = '<p class="note">Built from web pages. Each page passed the AI-defence screen first; passages ' \
                'marked [removed: …] were cut by it. Check anything important against the sources.</p>' \
        if task.get("untrusted") else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(t)}</title>
<style>{PAGE_CSS}</style></head><body><main class="sheet">
<div class="tools">{built}<a href="/doc/{task['id']}.docx">Download Word</a><a href="/#task={task['id']}">Ask a follow-up</a><a href="/">Back to the fleet</a><span class="meta">Print or save as PDF: your browser's Print (Ctrl+P, or Share → Print on a phone)</span></div>
<h1>{html.escape(t)}</h1><p class="meta">{html.escape(meta)}</p>{status}
<h2>Question</h2><div class="ask">{html.escape((task.get("ask") or ask_of(task["prompt"]))[:4000])}</div>
<h2>Answer</h2>
{to_html(answer) if answer.strip() else '<p class="meta">No answer yet.</p>'}
{src_html}{untrusted}
<p class="note">Made by the gpu-server fleet {html.escape(datetime.now().strftime("%Y-%m-%d %H:%M"))}.</p>
</main></body></html>"""


# ---------------- Word (.docx) ----------------

def _x(t):
    return html.escape(t, quote=False).replace("\u0000", "")


def _w_runs(text, rels, style_extra=""):
    out = []
    for t, flags, href in runs(text):
        rpr = ("<w:b/>" if "b" in flags else "") + ("<w:i/>" if "i" in flags else "") + \
              ('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>' if "code" in flags else "") + style_extra
        r = f'<w:r>{f"<w:rPr>{rpr}</w:rPr>" if rpr else ""}<w:t xml:space="preserve">{_x(t)}</w:t></w:r>'
        if href:
            rid = f"rId{100 + len(rels)}"
            rels.append((rid, href))
            r = (f'<w:hyperlink r:id="{rid}"><w:r><w:rPr><w:rStyle w:val="Hyperlink"/>{rpr}</w:rPr>'
                 f'<w:t xml:space="preserve">{_x(t)}</w:t></w:r></w:hyperlink>')
        out.append(r)
    return "".join(out)


def _p(inner, style=None, num=None):
    ppr = (f'<w:pStyle w:val="{style}"/>' if style else "") + \
          (f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num}"/></w:numPr>' if num else "")
    return f'<w:p>{f"<w:pPr>{ppr}</w:pPr>" if ppr else ""}{inner}</w:p>'


def docx(task):
    """The same document as a Word file (bytes)."""
    rels, body = [], []
    body.append(_p(_w_runs(title_for(task), rels), "Title"))
    meta = " · ".join(x for x in (f"#{task['id']} {task['kind']}", (task.get("updated") or "").replace("T", " ")[:16],
                                    f"answered by {task['by']}" if task.get("by") else "") if x)
    body.append(_p(_w_runs(meta, rels, '<w:color w:val="6B6B70"/><w:sz w:val="18"/>')))
    body.append(_p(_w_runs("Question", rels), "Heading1"))
    for line in (task.get("ask") or ask_of(task["prompt"]))[:4000].splitlines() or [""]:
        body.append(_p(_w_runs(line, rels, "<w:i/>"), "Quote"))
    body.append(_p(_w_runs("Answer", rels), "Heading1"))
    for kind, d in blocks(task.get("result") or task.get("partial") or ""):
        if kind == "h":
            body.append(_p(_w_runs(d[1], rels), f"Heading{min(d[0] + 1, 3)}"))
        elif kind == "p":
            body.append(_p(_w_runs(d, rels)))
        elif kind in ("ul", "ol"):
            for item in d:
                body.append(_p(_w_runs(item, rels), "ListParagraph", 1 if kind == "ul" else 2))
        elif kind == "code":
            for line in d.splitlines() or [""]:
                body.append(_p(f'<w:r><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="18"/></w:rPr>'
                               f'<w:t xml:space="preserve">{_x(line)}</w:t></w:r>'))
        elif kind == "quote":
            body.append(_p(_w_runs(d, rels), "Quote"))
        elif kind == "table" and d:
            rows = "".join("<w:tr>" + "".join(
                f'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>{_p(_w_runs(c, rels, "<w:b/>" if ri == 0 else ""))}</w:tc>'
                for c in r) + "</w:tr>" for ri, r in enumerate(d))
            body.append('<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
                        '<w:tblBorders>' + "".join(f'<w:{s} w:val="single" w:sz="4" w:color="BBBBBB"/>'
                                                   for s in ("top", "left", "bottom", "right", "insideH", "insideV")) +
                        f"</w:tblBorders></w:tblPr>{rows}</w:tbl>")
        elif kind == "hr":
            body.append(_p(""))
    src = sources(task)
    if src:
        body.append(_p(_w_runs("Sources read", rels), "Heading1"))
        for u, w in src:
            body.append(_p(_w_runs(u, rels) + _w_runs(f" ({w})", rels, '<w:color w:val="6B6B70"/>'), "ListParagraph", 2))
    if task.get("untrusted"):
        body.append(_p(_w_runs("Built from web pages that passed the AI-defence screen; check anything important "
                               "against the sources.", rels, '<w:i/><w:color w:val="6B6B70"/>')))
    ns = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {ns}><w:body>{"".join(body)}'
                '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
                'w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr></w:body></w:document>')
    links = "".join(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                    f'hyperlink" Target="{html.escape(h, quote=True)}" TargetMode="External"/>' for rid, h in rels)
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.'
                'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
                'openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship '
                'Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" '
                f'Target="numbering.xml"/>{links}</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", STYLES)
        z.writestr("word/numbering.xml", NUMBERING)
    return buf.getvalue()


CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/'
                 'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-'
                 'package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override '
                 'PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 'wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/'
                 'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/'
                 'numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
                 'numbering+xml"/></Types>')
ROOT_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.'
             'org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
             'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')


def _style(sid, name, ppr="", rpr="", based="Normal"):
    return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/><w:basedOn w:val="{based}"/>'
            f'<w:qFormat/><w:pPr>{ppr}</w:pPr><w:rPr>{rpr}</w:rPr></w:style>')


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="http://schemas.openxmlformats.org/'
          'wordprocessingml/2006/main"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/>'
          '<w:sz w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" '
          'w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
          '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
          + _style("Title", "Title", '<w:spacing w:after="60"/>', '<w:b/><w:sz w:val="40"/>')
          + _style("Heading1", "heading 1", '<w:keepNext/><w:spacing w:before="280" w:after="80"/><w:outlineLvl w:val="0"/>',
                   '<w:b/><w:sz w:val="30"/>')
          + _style("Heading2", "heading 2", '<w:keepNext/><w:spacing w:before="220" w:after="60"/><w:outlineLvl w:val="1"/>',
                   '<w:b/><w:sz w:val="26"/>')
          + _style("Heading3", "heading 3", '<w:keepNext/><w:spacing w:before="180" w:after="40"/><w:outlineLvl w:val="2"/>',
                   '<w:b/><w:i/><w:sz w:val="23"/>')
          + _style("Quote", "Quote", '<w:ind w:left="360"/>', '<w:color w:val="555555"/>')
          + _style("ListParagraph", "List Paragraph", '<w:ind w:left="720"/><w:spacing w:after="40"/>')
          + '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/><w:rPr><w:color w:val="0B57D0"/>'
            '<w:u w:val="single"/></w:rPr></w:style>'
          + '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblCellMar>'
            '<w:left w:w="80" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
          + '</w:styles>')

NUMBERING = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:numbering xmlns:w="http://schemas.openxmlformats.'
             'org/wordprocessingml/2006/main"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
             '<w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" '
             'w:hanging="360"/></w:pPr></w:lvl></w:abstractNum><w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0">'
             '<w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/><w:pPr>'
             '<w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId '
             'w:val="0"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num></w:numbering>')


# The document page runs no script at all and loads nothing from anywhere.
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"

# A web page the answer built (a ```html block holding a whole document), served on its own:
# its scripts may run, but in an opaque sandbox origin that can't fetch, post forms, or reach the fleet's page.
BUILT_CSP = ("sandbox allow-scripts allow-popups; default-src 'none'; script-src 'unsafe-inline'; "
             "style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; form-action 'none'; "
             "frame-ancestors 'none'; base-uri 'none'")
BUILT = re.compile(r"```(?:html|htm)?\s*\n(\s*(?:<!doctype html|<html)[\s\S]*?</html>)\s*\n```", re.IGNORECASE)


def built_page(text):
    """The whole HTML document an answer contains, or None."""
    m = BUILT.search(text or "")
    return m.group(1).strip() if m else None

