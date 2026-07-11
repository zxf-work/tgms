"""Interactive demo GUI (D-015): `tgms webapp --store S --suite suite.json
--model M --api-base ...` serves a single-page guided tour of TGMS:

  1. the dataset (card + stats)
  2. verified operators — a playground with preset calls, live envelopes
  3. ask the agent — curated test cases with expected gold, plus free-form;
     every ask links to a rendered trace page
  4. tamper demo — falsify a claim in a real answer, watch the verifier
     catch it (the C2 mechanism, on demand)
  5. time travel — a correction-probe pair executed deterministically
     (no LLM), showing as_of_tt belief-state queries

Engineering constraints: stdlib-only HTTP server (no new dependencies,
spec §8.6); binds 127.0.0.1 — remote access via SSH port forwarding;
store access is read-only and serialized behind a lock (DuckDB connections
are not thread-safe under the threading server).
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tgms.agent.agent import Agent, dataset_card
from tgms.agent.executor import Executor, ResultStore
from tgms.agent.ir import Plan
from tgms.agent.reporter import Reporter
from tgms.agent.verifier import ClaimVerifier
from tgms.store import Store
from tgms.tools.server import ToolRouter
from tgms.tools.trace_viewer import render_trace_html


class DemoApp:
    def __init__(self, store: Store, suite: dict[str, Any], model: str,
                 llm_fn, results_dir) -> None:
        self.store = store
        self.suite = suite
        self.model = model
        self.llm_fn = llm_fn
        self.router = ToolRouter(store.adapter)
        self.results = ResultStore(results_dir)
        self.records: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()  # DuckDB conn + agent are not thread-safe
        self.card = dataset_card(store)

    # ---------------- curated demo content ---------------------------------- #

    def examples(self) -> list[dict[str, Any]]:
        """Curated ask cases from the dev split, with program-computed gold."""
        tasks = self.suite["dev"] + self.suite["test"]
        picks: list[dict[str, Any]] = []

        def add(pred, why, limit=1):
            n = 0
            for t in tasks:
                if n >= limit:
                    break
                if pred(t):
                    picks.append({"id": t["id"], "question": t["question_text"],
                                  "family": t["family"], "gold": t["gold"],
                                  "kind": t["answer_kind"],
                                  "input_uids": t["input_uids"], "why": why})
                    n += 1

        add(lambda t: "reach_count" in t["id"],
            "Time-respecting reachability — vector/static RAG cannot compute this.")
        add(lambda t: "busiest" in t["id"],
            "Bucketed aggregation + top-k via the compute operator (no LLM arithmetic).")
        add(lambda t: "-probe-before-" in t["id"],
            "Bi-temporal: pins the belief state BEFORE a correction (as_of_tt).")
        add(lambda t: "-probe-now-" in t["id"],
            "Same entity under CURRENT beliefs — the answer differs from the pinned one.")
        add(lambda t: t["family"] == "t4" and "reach-motifs" in
            t["oracle_plan"]["plan_id"],
            "Multi-step: reachable set feeds delta-motif counting via $refs.")
        return picks

    def op_presets(self) -> list[dict[str, Any]]:
        t0, t1 = self.card["extent"]["vt_min"], self.card["extent"]["vt_max"]
        span = t1 - t0
        uid = self.store.adapter.uids_for([0])[0]
        return [
            {"label": "entity_history — one node's believed versions + edges",
             "op": "entity_history",
             "args": {"uid": uid, "include_edges": True, "limit": 5}},
            {"label": "temporal_reachability — who can be reached, and when",
             "op": "temporal_reachability",
             "args": {"src": uid, "window": {"t_a": t0, "t_b": t0 + span // 4},
                      "limit": 10}},
            {"label": "burst_detection — z-score outlier buckets",
             "op": "burst_detection",
             "args": {"target": {"kind": "edge_event_rate"},
                      "window": {"t_a": t0, "t_b": t1},
                      "stride": max(1, span // 60), "limit": 5}},
            {"label": "count_temporal_motifs — E_COST guardrail demo "
                      "(no node_filter at full window)",
             "op": "count_temporal_motifs",
             "args": {"motif": "M_triangle_cyclic", "delta": span // 30,
                      "window": {"t_a": t0, "t_b": t1}}},
            {"label": "diff_snapshots — what changed between two instants",
             "op": "diff_snapshots",
             "args": {"t1": t0 + span // 4, "t2": t0 + 3 * span // 4,
                      "limit": 5}},
        ]

    # ---------------- actions ------------------------------------------------ #

    def run_op(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            return self.router.call(op, args)

    def ask(self, question: str, input_uids: list[str]) -> dict[str, Any]:
        with self.lock:
            agent = Agent(self.store, model=self.model, llm_fn=self.llm_fn)
            out = agent.ask(question, task_input_uids=set(input_uids))
            trace, plan = out["trace"], out["plan_result"].plan
            rid = uuid.uuid4().hex[:12]
            record: dict[str, Any] = {
                "question": question, "answer": out["answer"],
                "pvr_first_emission": out["plan_result"].first_emission_valid,
                "n_attempts": len(out["plan_result"].attempts),
            }
            if plan is not None and trace is not None:
                reporter = Reporter(self.model, llm_fn=self.llm_fn)
                ao = reporter.report(question, plan, trace,
                                     agent.executor.results)
                report = ClaimVerifier(trace, agent.executor.results,
                                       self.store.adapter).verify(ao)
                record.update(plan=plan.to_json(), trace=trace.to_json(),
                              answer_object=ao, verifier_report=report,
                              receipts=f"model {self.model}; demo record {rid}")
                # keep verifier context for the tamper demo
                self.records[rid] = {"record": record,
                                     "verifier": ClaimVerifier(
                                         trace, agent.executor.results,
                                         self.store.adapter)}
            summary = {
                "record_id": rid,
                "answer": record.get("answer"),
                "text": record.get("answer_object", {}).get("text"),
                "pvr_first_emission": record["pvr_first_emission"],
                "n_attempts": record["n_attempts"],
                "executed_ok": bool(trace and trace.ok),
                "claims": [
                    {**{k: v for k, v in c.items() if k != "evidence"},
                     "evidence": c.get("evidence", []),
                     "verdict": next((r["verdict"] for r in
                                      record.get("verifier_report", {})
                                      .get("claims", [])
                                      if r["id"] == c["id"]), None)}
                    for c in record.get("answer_object", {}).get("claims", [])],
                "trace_url": f"/trace/{rid}" if rid in self.records else None,
            }
            return summary

    def tamper(self, record_id: str, claim_id: str) -> dict[str, Any]:
        entry = self.records.get(record_id)
        if entry is None:
            return {"error": "unknown record"}
        import copy
        ao = copy.deepcopy(entry["record"]["answer_object"])
        target = next((c for c in ao["claims"] if c["id"] == claim_id), None)
        if target is None:
            return {"error": "unknown claim"}
        note = ""
        if isinstance(target.get("value"), (int, float)) \
                and not isinstance(target.get("value"), bool):
            target["value"] = target["value"] + 1
            note = f"value perturbed to {target['value']} (+1)"
        elif target.get("uids"):
            target["uids"] = ["fabricated-node"] + target["uids"][1:]
            note = "first uid swapped for 'fabricated-node'"
        else:
            return {"error": "claim not tamperable (no numeric value or uids)"}
        with self.lock:
            report = entry["verifier"].verify(ao)
        return {"tampered": note,
                "verdicts": [{"id": c["id"], "verdict": c["verdict"],
                              "reason": c["reason"]} for c in report["claims"]]}

    def probe_demo(self) -> dict[str, Any]:
        """Deterministic bi-temporal demo: execute a before/now probe pair's
        oracle plans directly — no LLM involved."""
        tasks = self.suite["dev"] + self.suite["test"]
        pairs: dict[str, dict[str, Any]] = {}
        for t in tasks:
            if t["family"] != "probe":
                continue
            mode = "before" if "-before-" in t["id"] else "now"
            pairs.setdefault(t["input_uids"][0], {})[mode] = t
        pair = next((p for p in pairs.values() if len(p) == 2), None)
        if pair is None:
            return {"error": "no probe pair in suite"}
        out = {}
        with self.lock:
            for mode, t in sorted(pair.items()):
                plan = Plan.from_json(t["oracle_plan"])
                trace = Executor(self.router, self.results).run(plan)
                out[mode] = {"question": t["question_text"],
                             "answer": trace.answer, "gold": t["gold"],
                             "as_of_tt": t["oracle_plan"]["steps"][0]["args"]
                             .get("as_of_tt", "current")}
        out["explanation"] = (
            "Same entity, same operator chain — only as_of_tt differs. The "
            "'before' query pins the belief state prior to an injected "
            "correction; the 'now' query sees current beliefs. No snapshot "
            "or RAG baseline can express this distinction.")
        return out

    def trace_html(self, record_id: str) -> str | None:
        entry = self.records.get(record_id)
        if entry is None or "plan" not in entry["record"]:
            return None
        return render_trace_html(entry["record"])


# --------------------------------------------------------------------------- #
# HTTP layer                                                                   #
# --------------------------------------------------------------------------- #

def make_handler(app: DemoApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str).encode(),
                       "application/json")

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/info":
                self._json({"card": app.card, "model": app.model,
                            "tools": app.router.tools(),
                            "presets": app.op_presets(),
                            "examples": app.examples()})
            elif self.path == "/api/probe-demo":
                self._json(app.probe_demo())
            elif self.path.startswith("/trace/"):
                page = app.trace_html(self.path.split("/trace/")[1])
                if page is None:
                    self._send(404, b"no such trace", "text/plain")
                else:
                    self._send(200, page.encode(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            try:
                if self.path == "/api/op":
                    self._json(app.run_op(body["op"], body.get("args", {})))
                elif self.path == "/api/ask":
                    self._json(app.ask(body["question"],
                                       body.get("input_uids", [])))
                elif self.path == "/api/tamper":
                    self._json(app.tamper(body["record_id"], body["claim_id"]))
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:  # demo server: surface, don't die
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    return Handler


def serve(store: Store, suite: dict[str, Any], model: str, llm_fn,
          results_dir, host: str = "127.0.0.1", port: int = 8080) -> None:
    app = DemoApp(store, suite, model, llm_fn, results_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(json.dumps({"serving": f"http://{host}:{port}", "model": model,
                      "hint": "from your machine: ssh -N -L "
                              f"{port}:localhost:{port} <user>@<server>"}))
    httpd.serve_forever()


# --------------------------------------------------------------------------- #
# the single-page guided tour (no external assets)                             #
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>TGMS — guided demo</title><style>
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;
     background:#f6f7f9;color:#1c2733}
.wrap{max-width:1080px;margin:0 auto;padding:22px}
h1{font-size:21px;margin:8px 0} h2{font-size:15px;color:#44546a;margin:6px 0}
.step{background:#fff;border:1px solid #dfe3e8;border-radius:12px;
      padding:16px 20px;margin:16px 0}
.step .n{display:inline-block;background:#1c64d9;color:#fff;border-radius:50%;
      width:24px;height:24px;text-align:center;line-height:24px;font-size:13px;
      margin-right:8px}
.muted{color:#5b6b7c;font-size:13px}
button{background:#1c64d9;color:#fff;border:0;border-radius:8px;
       padding:7px 14px;font-size:13px;cursor:pointer;margin:4px 4px 4px 0}
button.sec{background:#eef1f5;color:#1c2733}
button:disabled{opacity:.5;cursor:wait}
select,textarea,input{font-family:ui-monospace,monospace;font-size:12px;
       width:100%;box-sizing:border-box;border:1px solid #cfd6dd;
       border-radius:8px;padding:8px;margin:6px 0}
textarea{min-height:90px}
pre{background:#f2f4f7;border-radius:8px;padding:10px;font-size:12px;
    overflow-x:auto;white-space:pre-wrap;word-break:break-word;max-height:320px;
    overflow-y:auto}
.badge{display:inline-block;border-radius:12px;color:#fff;font-size:11px;
       padding:2px 10px;margin:2px 6px 2px 0}
.ok{background:#0a7d33}.warn{background:#b58900}.bad{background:#c0392b}
.gray{background:#7f8c8d}
.case{border:1px solid #e3e8ee;border-radius:8px;padding:10px 12px;margin:8px 0;
      background:#fbfcfe}
.spinner{display:none;color:#1c64d9;font-size:13px}
.gold{font-size:12px;color:#0a7d33}
a{color:#1c64d9}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
</style></head><body><div class="wrap">
<h1>TGMS — agent-native bi-temporal graph store · guided demo</h1>
<div class="muted" id="hdr">loading…</div>

<div class="step"><span class="n">1</span><b>The data.</b>
<span class="muted">A bi-temporal event graph: every fact carries a valid-time
interval (when it held) and a transaction-time interval (when the store
believed it).</span>
<pre id="card"></pre></div>

<div class="step"><span class="n">2</span><b>Verified operators.</b>
<span class="muted">Thirteen typed, deterministic, cost-guarded tools — the
only way anything reads this store. Pick a preset, edit the JSON args, run
it, and inspect the self-describing envelope (note <code>result_digest</code>
— the verifier pins claims to these). The motif preset intentionally trips
the <b>E_COST guardrail</b> to show cost refusal with repair suggestions.</span>
<select id="preset"></select>
<textarea id="opargs"></textarea>
<button onclick="runOp()">Run operator</button>
<span class="spinner" id="opspin">running…</span>
<pre id="opout"></pre></div>

<div class="step"><span class="n">3</span><b>Ask the agent.</b>
<span class="muted">The LLM plans over the operators (it never sees raw data);
the plan is statically verified, executed deterministically, and the written
answer's claims are machine-checked. Curated cases show the
<b>program-computed expected answer</b> so you can check it yourself.
An ask takes 30–120&nbsp;s on the local 14B model.</span>
<div id="cases"></div>
<textarea id="q" placeholder="…or type your own question (mention entities by uid, e.g. n9)"></textarea>
<button onclick="ask()">Ask</button>
<span class="spinner" id="askspin">planning → executing → verifying… (up to ~2 min)</span>
<div id="askout"></div></div>

<div class="step"><span class="n">4</span><b>Tamper with a claim — watch the
verifier catch it.</b>
<span class="muted">This is the C2 mechanism live: falsify one claim from the
answer above (+1 a count, or swap an entity id) and re-verify against the
execution trace. 500/500 injected faults were caught in the acceptance run.</span>
<div id="tamper"><span class="muted">run an ask above first…</span></div></div>

<div class="step"><span class="n">5</span><b>Time travel (bi-temporal).</b>
<span class="muted">A correction was injected into this store's history. The
same operator chain, pinned to <code>as_of_tt</code> before the correction vs
now, returns different answers — deterministically, no LLM involved. No
snapshot or RAG system can express this question.</span>
<button onclick="probeDemo()">Run the before/now probe pair</button>
<span class="spinner" id="probespin">running…</span>
<div id="probeout"></div></div>

<div class="muted">TGMS demo server · read-only store · all answers carry
determinism receipts · see docs/TECHNICAL_REPORT.md</div>
</div><script>
let INFO=null, LAST=null;
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function j(url,opts){const r=await fetch(url,opts);return r.json();}
async function init(){
  INFO=await j('/api/info');
  $('hdr').textContent=`store: ${INFO.card.dataset||'store'} · ${INFO.card.n_entities} entities · ${INFO.card.n_edge_versions} edge versions · model: ${INFO.model} · ${INFO.tools.length} tools`;
  $('card').textContent=JSON.stringify(INFO.card,null,1);
  const sel=$('preset');
  INFO.presets.forEach((p,i)=>{const o=document.createElement('option');o.value=i;o.textContent=p.label;sel.appendChild(o);});
  sel.onchange=()=>{$('opargs').value=JSON.stringify(INFO.presets[sel.value].args,null,1);};
  sel.onchange();
  const cs=$('cases');
  INFO.examples.forEach((e,i)=>{
    const d=document.createElement('div');d.className='case';
    d.innerHTML=`<b>${esc(e.family)}</b> — ${esc(e.why)}<br>
      <span class="muted">${esc(e.question)}</span><br>
      <span class="gold">expected (program-computed): ${esc(JSON.stringify(e.gold))}</span><br>
      <button onclick='useCase(${i})'>Ask this</button>`;
    cs.appendChild(d);});
}
function useCase(i){const e=INFO.examples[i];$('q').value=e.question;$('q').dataset.uids=JSON.stringify(e.input_uids);$('q').dataset.gold=JSON.stringify(e.gold);ask();}
async function runOp(){
  const p=INFO.presets[$('preset').value];
  $('opspin').style.display='inline';$('opout').textContent='';
  try{const res=await j('/api/op',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({op:p.op,args:JSON.parse($('opargs').value)})});
    $('opout').textContent=JSON.stringify(res,null,1);}
  catch(e){$('opout').textContent=String(e);}
  $('opspin').style.display='none';
}
async function ask(){
  const q=$('q').value.trim(); if(!q)return;
  const uids=JSON.parse($('q').dataset.uids||'[]');
  const gold=$('q').dataset.gold;
  $('askspin').style.display='inline';$('askout').innerHTML='';
  const btns=document.querySelectorAll('button');btns.forEach(b=>b.disabled=true);
  try{
    const r=await j('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:q,input_uids:uids})});
    LAST=r;
    let goldNote='';
    if(gold!==undefined&&gold!==null&&gold!==''){
      const match=JSON.stringify(r.answer)===gold;
      goldNote=`<div class="gold">expected ${esc(gold)} → got ${esc(JSON.stringify(r.answer))} ${match?'✓ MATCH':'✗ (see trace for why)'}</div>`;}
    const claims=(r.claims||[]).map(c=>{
      const cls=c.verdict==='supported'?'ok':(c.verdict==='weakly_supported'?'warn':(c.verdict==='unsupported'?'bad':'gray'));
      return `<span class="badge ${cls}">${esc(c.id)}: ${esc(c.verdict||'—')}</span>`;}).join(' ');
    $('askout').innerHTML=`<div class="case">
      <b>answer:</b> ${esc(r.text||JSON.stringify(r.answer))}<br>${goldNote}
      <span class="muted">first-emission plan valid: ${r.pvr_first_emission} ·
      attempts: ${r.n_attempts} · executed: ${r.executed_ok}</span><br>
      ${claims} ${r.trace_url?`· <a href="${r.trace_url}" target="_blank">open full trace ↗</a>`:''}</div>`;
    renderTamper();
  }catch(e){$('askout').innerHTML='<pre>'+esc(String(e))+'</pre>';}
  $('askspin').style.display='none';btns.forEach(b=>b.disabled=false);
  $('q').dataset.gold='';$('q').dataset.uids='[]';
}
function renderTamper(){
  if(!LAST||!(LAST.claims||[]).length){$('tamper').innerHTML='<span class="muted">the last ask produced no machine-checkable claims…</span>';return;}
  $('tamper').innerHTML=LAST.claims.map(c=>
    `<div class="case">claim <b>${esc(c.id)}</b> (${esc(c.type)}) — currently
     <span class="badge ${c.verdict==='supported'?'ok':'gray'}">${esc(c.verdict||'—')}</span>
     <button class="sec" onclick="tamper('${esc(c.id)}')">falsify this claim</button>
     <span id="t-${esc(c.id)}"></span></div>`).join('');
}
async function tamper(cid){
  const r=await j('/api/tamper',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({record_id:LAST.record_id,claim_id:cid})});
  const el=$('t-'+cid);
  if(r.error){el.innerHTML='<span class="muted">'+esc(r.error)+'</span>';return;}
  const v=r.verdicts.find(x=>x.id===cid)||{};
  const cls=v.verdict==='supported'?'ok':'bad';
  el.innerHTML=` → ${esc(r.tampered)} → verifier says
    <span class="badge ${cls}">${esc(v.verdict)}</span>
    <span class="muted">${esc(v.reason||'')}</span>`;
}
async function probeDemo(){
  $('probespin').style.display='inline';$('probeout').innerHTML='';
  const r=await j('/api/probe-demo');
  $('probespin').style.display='none';
  if(r.error){$('probeout').textContent=r.error;return;}
  $('probeout').innerHTML=`<div class="grid">
    <div class="case"><b>as of tt=${esc(r.before.as_of_tt)} (before the correction)</b><br>
      <span class="muted">${esc(r.before.question)}</span><br>
      answer: <b>${esc(JSON.stringify(r.before.answer))}</b>
      <span class="gold">(gold ${esc(JSON.stringify(r.before.gold))})</span></div>
    <div class="case"><b>current beliefs</b><br>
      <span class="muted">${esc(r.now.question)}</span><br>
      answer: <b>${esc(JSON.stringify(r.now.answer))}</b>
      <span class="gold">(gold ${esc(JSON.stringify(r.now.gold))})</span></div>
    </div><div class="muted">${esc(r.explanation)}</div>`;
}
init();
</script></body></html>
"""
