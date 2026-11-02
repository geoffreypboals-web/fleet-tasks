"""screen.py -- the AI-defence screen for text nobody here wrote (board WP-152 step 3c).

Web pages read by a research task (and, later, emails or other people's
repos) can carry instructions aimed at the model: "ignore your previous
instructions and ...". Before any model sees such text, scan() finds those
passages and clean() cuts them out, masks personal data and keys, or tells
the caller to drop the page.

The patterns are ported from Ruflo's AI-defence detector
(ruvnet/claude-flow, v3/@claude-flow/aidefence, threat-detection-service.ts),
with the 2.3.0 fixes (up to four words between "ignore" and "instructions",
role hijack, jailbreak markers), and tuned for web pages: patterns that are
ordinary on a web page ("base64", "hypothetically", "system:", "{{ }}")
are only logged, never cut.

  Copyright (c) 2024-2026 ruvnet. MIT License. Permission is hereby granted,
  free of charge, to any person obtaining a copy of this software and
  associated documentation files, to deal in the Software without
  restriction, subject to including this notice. THE SOFTWARE IS PROVIDED
  "AS IS", WITHOUT WARRANTY OF ANY KIND.

Standard library only; nothing here calls a model or the network.
"""
import re
import unicodedata

# (name, category, severity, pattern). severity "cut" removes the passage;
# "log" only records it (common on ordinary pages).
PATTERNS = [
    ("ignore_instructions", "instruction_override", "cut",
     r"\b(?:ignore|disregard|forget|override)\s+(?:(?:the|of|these|those|my|every)\s+){0,3}"
     r"(?:all|any|your|previous|prior|above|earlier|preceding|original|initial|system|existing|other)\s+"
     r"(?:(?:the|of|your|previous|prior|above|earlier|preceding|system|initial|original|other)\s+){0,3}"
     r"(?:instructions?|prompts?|rules|guidelines|directions|directives)\b"),
    ("forget_everything", "instruction_override", "cut",
     r"\bforget\s+(?:everything|all\s+(?:of\s+)?(?:the\s+|your\s+)?(?:above|previous|prior))\b"),
    ("disregard_above", "instruction_override", "cut", r"\bdisregard\s+(?:all\s+)?(?:of\s+)?(?:the\s+)?(?:previous|prior|above)\b"),
    ("do_not_follow", "instruction_override", "cut", r"\bdo\s+not\s+follow\s+(?:the\s+)?(?:previous|above|prior)\b"),
    ("new_instructions", "instruction_override", "cut",
     r"\b(?:new|updated|real|actual)\s+instructions?\s*(?::|for\s+(?:the\s+)?(?:ai|assistant|model|llm))"),
    ("addressed_to_ai", "instruction_override", "cut",
     r"\b(?:ai|assistant|llm|language\s+model|chatbot|claude|gpt|gemini)s?\s*[,:]\s*(?:you\s+must|ignore|please\s+ignore|do\s+not|don't|stop|instead)\b"),
    ("you_are_now", "role_switching", "cut", r"\byou\s+are\s+now\s+(?!going\b|about\b|ready\b|able\b|logged\b|signed\b|subscribed\b|leaving\b|viewing\b|on\b|in\b|at\b|reading\b|watching\b|connected\b|registered\b)"),
    ("act_as_other", "role_switching", "cut", r"\bact\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?an?\s*(?:different|new|another|unrestricted|unfiltered)\b"),
    ("pretend", "role_switching", "log", r"\bpretend\s+(?:to\s+be|you\s+are)\b"),
    ("dan_mode", "jailbreak", "cut", r"\bDAN\b[^\n]{0,200}?\bmode\b|\bmode\b[^\n]{0,200}?\bDAN\b"),
    ("jailbreak_word", "jailbreak", "log", r"\bjailbr(?:eak|oken)\b"),
    ("bypass_rules", "jailbreak", "cut", r"\bbypass\s+(?:all\s+)?(?:your\s+|the\s+)?(?:restrictions|limitations|rules|filters|guardrails|safety)\b"),
    ("no_restrictions", "jailbreak", "log", r"\bwithout\s+(?:any\s+)?(?:restrictions|limitations|rules)\b"),
    ("disable_filter", "jailbreak", "cut", r"\bdisable\s+(?:your\s+|the\s+)?(?:safety\s+|content\s+)?(?:filters?|guardrails)\b"),
    ("dev_mode", "jailbreak", "log", r"\b(?:developer|dev|debug|god|root)\s+mode\b"),
    ("hidden_features", "jailbreak", "cut", r"\benable\s+(?:hidden|secret|special)\s+(?:features|mode|commands)\b"),
    ("fake_system_tag", "context_manipulation", "cut", r"<\|(?:system|im_start|im_end)\|>|<\s*/?\s*system\s*>|\[/?INST\]|<</?SYS>>"),
    ("system_line", "context_manipulation", "log", r"(?m)^\s*system\s*:\s*\S"),
    ("system_prompt_ref", "context_manipulation", "log", r"\[system\]|\{system\}|\bsystem\s+prompt\b"),
    ("reveal_prompt", "context_manipulation", "cut", r"\b(?:reveal|print|show|repeat|output)\s+(?:your|the)\s+(?:system\s+|initial\s+|hidden\s+)?(?:prompt|instructions)\b"),
    ("ask_instructions", "context_manipulation", "log", r"\bwhat\s+(?:is|are)\s+your\s+(?:initial\s+)?instructions\b"),
    ("bracketed_system", "context_manipulation", "cut", r"(?:\[\[|\{\{|<<)\s*(?:system|instructions?|prompt)\s*:"),
    ("bracket_injection", "context_manipulation", "log", r"\[\[[^\]\n]{0,200}\]\]|<<[^>\n]{0,200}>>|\{\{[^}\n]{0,200}\}\}"),
    ("block_delimiter", "context_manipulation", "cut", r"```(?:system|instruction|prompt)\b|---\s*(?:system|instruction)\s*---"),
    ("exfiltrate", "context_manipulation", "cut",
     r"\b(?:send|post|email|upload|exfiltrate)\s+(?:the\s+|your\s+|all\s+)?(?:conversation|chat|context|secrets?|api\s+keys?|credentials|passwords?)(?:\s+(?:history|log|so\s+far))?\s+to\b"),
    ("encoding", "encoding_attack", "log", r"\b(?:base64|rot13|hex\s+encode|url\s+encode)\b"),
    ("decode_this", "encoding_attack", "log", r"\bdecode\s+this\b|\bencrypted\s+message\b"),
    ("long_base64", "encoding_attack", "log", r"[A-Za-z0-9+/]{120,}={0,2}"),
    ("hypothetical", "prompt_injection", "log", r"\bhypothetically\b|\bif\s+you\s+could\b"),
    ("educational", "prompt_injection", "log", r"\bfor\s+(?:educational|research|academic)\s+purposes\b"),
]
COMPILED = [(n, cat, sev, re.compile(p, re.IGNORECASE)) for n, cat, sev, p in PATTERNS]

# Personal data and keys: masked, never cut. The key formats match fleet.py's secret scan.
PII = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b"), "[email]"),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[ssn]"),
    ("card", re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"), "[card number]"),
    ("key", re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|gh[opsu]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|"
                       r"AKIA[0-9A-Z]{16}|xox[abp]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})"), "[key]"),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), "[private key]"),
    ("password", re.compile(r"\bpassword\s*[:=]\s*[\"']?[^\"'\s]{4,}[\"']?", re.IGNORECASE), "password: [hidden]"),
]
DROP_CATEGORIES = {"jailbreak", "role_switching"}      # one cut-level hit of these drops the whole page
DROP_AFTER = 3                                         # more cut-level hits than this drops it too
INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿]")


def normalize(text):
    """What the patterns see: NFKC (fullwidth and lookalike letters become
    plain), invisible characters removed, runs of spaces collapsed."""
    t = INVISIBLE.sub("", unicodedata.normalize("NFKC", text or ""))
    return re.sub(r"[ \t ]+", " ", t)


def scan(text):
    """Every pattern hit: [{name, category, severity, match, start, end}], by position."""
    t = normalize(text)
    hits = []
    for name, cat, sev, rx in COMPILED:
        for m in rx.finditer(t):
            hits.append({"name": name, "category": cat, "severity": sev, "match": m.group(0)[:200],
                         "start": m.start(), "end": m.end()})
    hits.sort(key=lambda h: h["start"])
    return hits


_END = re.compile(r"[.!?](?=\s|$)|\n")


def _sentence(t, start, end):
    """The sentence or line around a hit, so the whole instruction goes, not
    just its trigger words. A full stop ends a sentence only before a space
    or the end (so 'evil@x.com' and '3.5' don't)."""
    left = max((m.end() for m in _END.finditer(t, 0, start)), default=0)
    m = _END.search(t, end)
    return left, (m.end() if m else len(t))


def clean(text):
    """(cleaned text or None to drop the page, hits, why). Cut-level hits
    are removed with their sentence and replaced by [removed: category];
    personal data and keys are masked; log-level hits stay in the text
    and are only reported."""
    t = normalize(text)
    hits = scan(t)
    cuts = [h for h in hits if h["severity"] == "cut"]
    if any(h["category"] in DROP_CATEGORIES for h in cuts):
        return None, hits, "a " + next(h["category"] for h in cuts if h["category"] in DROP_CATEGORIES).replace("_", " ") + " passage"
    if len(cuts) > DROP_AFTER:
        return None, hits, f"{len(cuts)} passages aimed at the AI"
    spans = []
    for h in cuts:
        a, b = _sentence(t, h["start"], h["end"])
        if spans and a <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(b, spans[-1][1]), spans[-1][2])
        else:
            spans.append((a, b, h["category"]))
    out, last = [], 0
    for a, b, cat in spans:
        out.append(t[last:a])
        out.append(f" [removed: {cat}] ")
        last = b
    out.append(t[last:])
    cleaned = "".join(out)
    for name, rx, mask in PII:
        found = rx.findall(cleaned)
        if found:
            hits.append({"name": name, "category": "personal_data", "severity": "mask", "match": f"{len(found)} masked",
                         "start": -1, "end": -1})
            cleaned = rx.sub(mask, cleaned)
    return cleaned, hits, ""


def summary(hits):
    """'2 cut (instruction override), 1 masked' style text for notes and approval cards."""
    cut = [h for h in hits if h["severity"] == "cut"]
    masked = [h for h in hits if h["severity"] == "mask"]
    logged = [h for h in hits if h["severity"] == "log"]
    parts = []
    if cut:
        parts.append(f"{len(cut)} cut (" + ", ".join(sorted({h['category'].replace('_', ' ') for h in cut})) + ")")
    if masked:
        parts.append("personal data/keys masked")
    if logged:
        parts.append(f"{len(logged)} noted")
    return ", ".join(parts) or "clean"
