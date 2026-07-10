"""Trace viewer (spec 7.3.4): `tgms trace render <record.json> -o trace.html`
emits a static, self-contained HTML file — question -> plan DAG -> per-step
operator cards (args, row counts, latency, truncation flags) -> final answer
with each claim badged and hyperlinked to its evidence step.

No server, no build step, no external assets: hand-rolled SVG for the DAG,
inline CSS. The navigation model is the product's: ask -> answer -> audit
the evidence. All data-derived strings are HTML-escaped.
"""

from __future__ import annotations

import html
import json
from typing import Any

BADGE = {
    "supported": ("verified", "#0a7d33"),
    "weakly_supported": ("weakly supported", "#b58900"),
    "unsupported": ("unsupported", "#c0392b"),
    "unverifiable": ("unverifiable", "#7f8c8d"),
}

_CSS = """
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;
     background:#f6f7f9;color:#1c2733}
.wrap{max-width:1060px;margin:0 auto;padding:24px}
h1{font-size:20px} h2{font-size:15px;margin:26px 0 10px;color:#44546a;
   text-transform:uppercase;letter-spacing:.06em}
.q{background:#fff;border:1px solid #dfe3e8;border-radius:10px;padding:14px 18px;
   font-size:16px}
.card{background:#fff;border:1px solid #dfe3e8;border-radius:10px;
      padding:12px 16px;margin:10px 0}
.card h3{margin:0 0 6px;font-size:14px;font-family:ui-monospace,monospace}
.meta{color:#5b6b7c;font-size:12px;margin-bottom:6px}
.meta b{color:#1c2733}
pre{background:#f2f4f7;border-radius:6px;padding:8px 10px;font-size:12px;
    overflow-x:auto;margin:6px 0;white-space:pre-wrap;word-break:break-word}
.badge{display:inline-block;border-radius:12px;color:#fff;font-size:11px;
       padding:2px 10px;margin-right:8px;vertical-align:middle}
.status-ok{color:#0a7d33;font-weight:600}
.status-failed,.status-skipped{color:#c0392b;font-weight:600}
.claim{border-left:4px solid #dfe3e8;padding:8px 12px;margin:8px 0;
       background:#fff;border-radius:0 8px 8px 0;border-top:1px solid #eef1f4;
       border-right:1px solid #eef1f4;border-bottom:1px solid #eef1f4}
.evidence a{font-family:ui-monospace,monospace;font-size:12px;margin-right:6px}
.answer{background:#eef7ef;border:1px solid #bfe3c6;border-radius:10px;
        padding:14px 18px;font-size:15px}
.receipts{color:#7f8c8d;font-size:11px;margin-top:28px;
          font-family:ui-monospace,monospace}
svg text{font-family:ui-monospace,monospace;font-size:12px}
"""


def _esc(x: Any) -> str:
    return html.escape(str(x), quote=True)


def _pre(obj: Any, limit: int = 1500) -> str:
    s = json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False)
    if len(s) > limit:
        s = s[:limit] + "\n…[truncated for display]"
    return f"<pre>{_esc(s)}</pre>"


def _dag_svg(plan: dict[str, Any], trace_by_id: dict[str, dict]) -> str:
    steps = plan["steps"]
    depth: dict[str, int] = {}
    for s in steps:  # steps arrive topologically ordered in the IR
        depth[s["id"]] = 1 + max((depth.get(d, 0) for d in s.get("depends_on", [])),
                                 default=0)
    cols: dict[int, list[str]] = {}
    for s in steps:
        cols.setdefault(depth[s["id"]], []).append(s["id"])
    W, H, GX, GY = 168, 46, 200, 68
    pos = {}
    for c, ids in sorted(cols.items()):
        for r, sid in enumerate(ids):
            pos[sid] = ((c - 1) * GX + 10, r * GY + 12)
    width = max(x for x, _ in pos.values()) + W + 12
    height = max(y for _, y in pos.values()) + H + 12
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" '
             f'style="max-width:100%">']
    for s in steps:  # edges under nodes
        x2, y2 = pos[s["id"]]
        for d in s.get("depends_on", []):
            x1, y1 = pos[d]
            parts.append(
                f'<path d="M {x1 + W} {y1 + H / 2} C {x1 + W + 34} {y1 + H / 2}, '
                f'{x2 - 34} {y2 + H / 2}, {x2} {y2 + H / 2}" '
                'fill="none" stroke="#9aa7b4" stroke-width="1.6"/>')
    for s in steps:
        x, y = pos[s["id"]]
        rec = trace_by_id.get(s["id"], {})
        color = {"ok": "#0a7d33", "failed": "#c0392b",
                 "skipped": "#b58900"}.get(rec.get("status"), "#7f8c8d")
        parts.append(
            f'<a href="#step-{_esc(s["id"])}">'
            f'<rect x="{x}" y="{y}" width="{W}" height="{H}" rx="9" fill="#fff" '
            f'stroke="{color}" stroke-width="2"/>'
            f'<text x="{x + 10}" y="{y + 19}" fill="#44546a">{_esc(s["id"])}</text>'
            f'<text x="{x + 10}" y="{y + 36}" fill="#1c2733">'
            f'{_esc(s["op"][:20])}</text></a>')
    parts.append("</svg>")
    return "".join(parts)


def render_trace_html(record: dict[str, Any]) -> str:
    """record: {question, plan, trace, answer, answer_object?, verifier_report?,
    receipts?} — the shape `tgms ask --save-record` writes."""
    plan = record["plan"]
    trace = record["trace"]
    trace_by_id = {s["step_id"]: s for s in trace["steps"]}
    verdicts = {c["id"]: c for c in
                (record.get("verifier_report") or {}).get("claims", [])}

    out = [f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<title>TGMS trace {_esc(plan.get('plan_id', ''))}</title>"
           f"<style>{_CSS}</style></head><body><div class='wrap'>",
           "<h1>TGMS execution trace</h1>",
           f"<div class='q'><b>Question.</b> {_esc(record.get('question', ''))}"
           "</div>",
           "<h2>Plan DAG</h2>", _dag_svg(plan, trace_by_id),
           "<h2>Steps</h2>"]

    for s in plan["steps"]:
        rec = trace_by_id.get(s["id"], {})
        status = rec.get("status", "not-run")
        out.append(
            f"<div class='card' id='step-{_esc(s['id'])}'>"
            f"<h3>{_esc(s['id'])} · {_esc(s['op'])} "
            f"<span class='status-{_esc(status)}'>{_esc(status)}</span></h3>"
            "<div class='meta'>"
            f"rows: <b>{_esc(rec.get('rows_returned', '—'))}</b> · "
            f"wall: <b>{_esc(rec.get('wall_ms', '—'))} ms</b> · "
            f"truncated: <b>{_esc(rec.get('truncated', False))}</b> · "
            f"digest: <b>{_esc(str(rec.get('result_digest', ''))[:16])}</b></div>"
            f"{_pre(s['args'])}"
            + (f"<div class='meta'>error:</div>{_pre(rec['error'])}"
               if rec.get("error") else "")
            + "</div>")

    out.append("<h2>Answer</h2>")
    ao = record.get("answer_object") or {}
    out.append(f"<div class='answer'>{_esc(ao.get('text', record.get('answer')))}"
               "</div>")
    if ao.get("claims"):
        out.append("<h2>Claims</h2>")
        for c in ao["claims"]:
            v = verdicts.get(c["id"], {})
            label, color = BADGE.get(v.get("verdict", "unverifiable"),
                                     BADGE["unverifiable"])
            ev = " ".join(f"<a href='#step-{_esc(e)}'>{_esc(e)}</a>"
                          for e in c.get("evidence", []))
            body = {k: val for k, val in c.items() if k not in ("id", "evidence")}
            out.append(
                f"<div class='claim' style='border-left-color:{color}'>"
                f"<span class='badge' style='background:{color}'>{label}</span>"
                f"<b>{_esc(c['id'])}</b> "
                f"<span class='evidence'>evidence: {ev}</span>"
                f"{_pre(body, 500)}"
                + (f"<div class='meta'>verifier: {_esc(v.get('reason', ''))}</div>"
                   if v else "") + "</div>")

    rec_line = record.get("receipts")
    if rec_line:
        out.append(f"<div class='receipts'>receipts — {_esc(rec_line)}</div>")
    out.append("</div></body></html>")
    return "".join(out)
