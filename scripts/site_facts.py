#!/usr/bin/env python3
"""Keep every number the public site asserts tied to one source.

The site drifted because the same fact was typed into a project page, a blog
index card, a post, an SVG label and its alt text, and only some of them were
updated when the fact changed. That is a publishing-pipeline problem, not a
writing problem, so the fix is a pipeline: facts live in `docs/site_facts.json`,
pages mark each quoted fact with `data-fact="<key>"`, and this script either
rewrites them from the source (`apply`) or fails when they disagree (`check`).

    uv run python scripts/site_facts.py check    # CI gate
    uv run python scripts/site_facts.py apply    # rewrite marked spans
    uv run python scripts/site_facts.py list     # what is available to quote

`check` also enforces `retired_phrases`: strings that were true once and must
never appear on the site again, each with the reason it was retired. That is
what stops a corrected claim from surviving in an index card or an alt text
after the post itself has been fixed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "docs" / "site_facts.json"
PAGES = sorted([*(ROOT / "docs").glob("*.html"), *(ROOT / "docs" / "blog").glob("*.html")])
#: Markdown cannot carry data-fact spans, but it can still repeat a retired
#: claim — and the README is the page most readers reach first.
PROSE = [ROOT / "README.md"]

#: <span data-fact="key">value</span> — the value is regenerated, never authored.
MARK = re.compile(r'(<([a-z]+)\s+[^>]*?data-fact="([a-z0-9_]+)"[^>]*>)(.*?)(</\2>)',
                  re.DOTALL)


def load() -> dict:
    return json.loads(FACTS.read_text())


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def check(data: dict) -> int:
    facts, bad = data["facts"], []
    retired = data.get("retired_phrases", [])
    seen: set[str] = set()

    for page in PAGES:
        text = page.read_text()
        rel = page.relative_to(ROOT)
        for m in MARK.finditer(text):
            key, shown = m.group(3), _strip_tags(m.group(4)).strip()
            seen.add(key)
            if key not in facts:
                bad.append(f"{rel}: data-fact=\"{key}\" is not in site_facts.json")
            elif shown != facts[key]["value"]:
                bad.append(f"{rel}: {key} shows {shown!r}, source says "
                           f"{facts[key]['value']!r} — run `site_facts.py apply`")
        for r in retired:
            if r["phrase"].lower() in text.lower():
                bad.append(f"{rel}: retired phrase {r['phrase']!r} — {r['reason']}")

    for doc in PROSE:
        if not doc.exists():
            continue
        text, rel = doc.read_text(), doc.relative_to(ROOT)
        for r in retired:
            if r["phrase"].lower() in text.lower():
                bad.append(f"{rel}: retired phrase {r['phrase']!r} — {r['reason']}")

    if bad:
        print("site facts: FAIL")
        for b in bad:
            print("  " + b)
        return 1
    print(f"site facts: {len(PAGES)} pages + {len(PROSE)} prose file(s) clean "
          f"({len(seen)} marked facts, {len(retired)} retired phrases)")
    return 0


def apply(data: dict) -> int:
    facts, changed = data["facts"], 0
    for page in PAGES:
        text = page.read_text()

        def sub(m: re.Match) -> str:
            key = m.group(3)
            if key not in facts:
                return m.group(0)
            return m.group(1) + facts[key]["value"] + m.group(5)

        new = MARK.sub(sub, text)
        if new != text:
            page.write_text(new)
            changed += 1
            print(f"  updated {page.relative_to(ROOT)}")
    print(f"site facts: rewrote {changed} page(s)")
    return 0


def show(data: dict) -> int:
    print(f"TGMS {data['tgms_version']} — facts updated {data['updated']}\n")
    for key, f in sorted(data["facts"].items()):
        prose = f.get("prose", "")
        print(f"  {key:<38} {f['value']:>8}   [{f.get('kind', '?')}] {prose}")
    print("\nsnapshots:")
    for key, s in data["snapshots"].items():
        print(f"  {key:<26} commit {s['commit']}  {s['scale']}")
    if data.get("retired_phrases"):
        print("\nretired (must not appear on the site):")
        for r in data["retired_phrases"]:
            print(f"  {r['phrase']!r} — {r['reason']}")
    return 0


def main() -> int:
    data = load()
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    return {"check": check, "apply": apply, "list": show}[mode](data)


if __name__ == "__main__":
    raise SystemExit(main())
