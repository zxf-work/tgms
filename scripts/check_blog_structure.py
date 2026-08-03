#!/usr/bin/env python3
"""Check published posts against docs/BLOG_STYLE.md §3.

The editorial standard is only worth writing down if something enforces the
parts of it a machine can see. This checks the structural contract — the
sections a reader relies on being there — and deliberately checks nothing
about prose quality, which is a review job.

    uv run python scripts/check_blog_structure.py

Rules, each traceable to a failure the audit found:
  * every post declares a track in its kicker, so its genre is visible;
  * every post states what its result does *not* show, because a measured
    result stated without a boundary reads as a universal property;
  * every post carries a changelog, because results change and the audit
    found stale headlines patched by paragraphs underneath them;
  * every post names its status, so a historical snapshot cannot be
    mistaken for a current one.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POSTS = sorted(p for p in (ROOT / "docs" / "blog").glob("*.html")
               if p.name != "index.html")

TRACKS = ("Understand TGMS", "Evidence &amp; capability", "Engineering case study")
#: A limitation section may be titled for what the post is — a page, a
#: benchmark, a result — so match the shape rather than one exact wording.
LIMIT = re.compile(r"does not (show|establish|prove|cover|tell)", re.I)


def headings(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", h).strip()
            for h in re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.S)]


def main() -> int:
    bad: list[str] = []
    for post in POSTS:
        html, name = post.read_text(), post.name
        hs = headings(html)

        kicker = re.search(r'class="kicker">(.*?)<', html)
        if not kicker:
            bad.append(f"{name}: no kicker")
        elif not any(t in kicker.group(1) for t in TRACKS):
            bad.append(f"{name}: kicker {kicker.group(1)!r} declares no track "
                       f"(one of {', '.join(TRACKS)})")

        if not any(LIMIT.search(h) for h in hs):
            bad.append(f"{name}: no limitation section — BLOG_STYLE §3 item 8 "
                       f"is not optional")
        if not any(h.lower().startswith("changelog") for h in hs):
            bad.append(f"{name}: no changelog section")
        if not re.search(r"status:\s*(current|updated result|historical snapshot)",
                          html, re.I):
            bad.append(f"{name}: no status (Current / Updated result / "
                       f"Historical snapshot)")

    if bad:
        print("blog structure: FAIL")
        for b in bad:
            print("  " + b)
        return 1
    print(f"blog structure: {len(POSTS)} posts conform to BLOG_STYLE §3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
