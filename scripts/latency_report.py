#!/usr/bin/env python3
"""
Prod latency report for twin sessions, built from Langfuse.

  python3 scripts/latency_report.py                 # every voice call since 10 Nov 2026, writes logs/latency-report.html
  python3 scripts/latency_report.py --days 2        # only the last 2 days
  python3 scripts/latency_report.py --cached        # re-render from the last pull, no Langfuse calls

Needs LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL in the environment or in chat/.env
(the same Langfuse project prod writes to). Only environment=production is read.

Sources (both export to Langfuse over OpenTelemetry):
  voice agent  service intervoo-agent (agent/src/telemetry.py): one trace per call. agent_turn carries
               lk.e2e_latency (learner stops speaking → twin starts speaking), lk.end_of_turn_delay;
               llm_request carries time to first chunk from the brain; tts_request the TTS first audio.
               Traced in prod since 10 Nov 2026 12:59 UTC, so the report starts there (TRACED_SINCE).
  chat brain   service chat (chat/agent/instrumentation/langfuse.ts): one trace per brain request
               (invoke_agent chat → agent.step → chat <model> + tool spans), tokens incl. cached.
               Each one is matched to the voice turn that sent it.

Not in Langfuse: the studio side (Start click → POST /api/sessions, uploads, finalize, end webhook,
twin report) and who ran the session. Load-test sessions (lt-…) and the local test agent are skipped.
Prompts and transcripts are not fetched.
"""

import argparse
import base64
import collections
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "logs" / "latency"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
VOICE_SERVICE = "intervoo-agent"
TOOLS_FILE = {"read_document"}
TRACED_SINCE = dt.datetime(2026, 11, 10, 12, 59, tzinfo=dt.UTC)  # voice agent → Langfuse went live in prod


# ---------------------------------------------------------------- pull

def langfuse_env():
    # Values pasted with their .env quotes (e.g. into GitHub secrets) still work.
    env = {k: os.environ[k].strip().strip("\"'") for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
           if os.environ.get(k, "").strip().strip("\"'")}
    for f in (ROOT / "chat" / ".env", Path.cwd() / "chat" / ".env"):
        if len(env) == 3 or not f.exists():
            continue
        for line in f.read_text().splitlines():
            m = re.match(r"^(LANGFUSE_(?:PUBLIC_KEY|SECRET_KEY|BASE_URL))=(.*)$", line.strip())
            if m:
                env.setdefault(m.group(1), m.group(2).strip().strip("\"'"))
    if len(env) < 3:
        sys.exit("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL not found (env or chat/.env)")
    return env


def pull(days: float | None, out: Path) -> int:
    """Pages through GET /api/public/v2/observations (cursor) for production spans in the window."""
    env = langfuse_env()
    auth = base64.b64encode(f"{env['LANGFUSE_PUBLIC_KEY']}:{env['LANGFUSE_SECRET_KEY']}".encode()).decode()
    to = dt.datetime.now(dt.UTC)
    frm = max(TRACED_SINCE, to - dt.timedelta(days=days)) if days else TRACED_SINCE
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    rows, cursor = [], None
    while True:
        q = {"fields": "core,basic,metadata,usage", "fromStartTime": iso(frm), "toStartTime": iso(to),
             "limit": 1000, "environment": "production"}
        if cursor:
            q["cursor"] = cursor
        req = urllib.request.Request(f"{env['LANGFUSE_BASE_URL'].rstrip('/')}/api/public/v2/observations?{urllib.parse.urlencode(q)}",
                                     headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=180) as res:
            page = json.load(res)
        rows += page.get("data", [])
        cursor = (page.get("meta") or {}).get("cursor")
        print(f"  langfuse: {len(rows)} spans", file=sys.stderr)
        if not cursor or not page.get("data"):
            break
    out.write_text(json.dumps({"from": iso(frm), "to": iso(to), "rows": rows}))
    return len(rows)


# ---------------------------------------------------------------- stats

def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return round(xs[f] + (xs[c] - xs[f]) * (k - f))


def summ(xs):
    xs = [x for x in xs if x is not None]
    return {"n": len(xs), "p50": pct(xs, .5), "p80": pct(xs, .8),
            "min": min(xs) if xs else None, "max": max(xs) if xs else None}


# ---------------------------------------------------------------- analyze

def ms(iso):
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000) if iso else None


def secs(x):
    return None if x is None else round(float(x) * 1000)


def metric(value, key):
    """lk.*_metrics come back as a dict, or as a JSON string Langfuse cuts at 200 chars: read the number either way."""
    if isinstance(value, dict):
        return value.get(key)
    m = re.search(rf'"{key}":\s*(-?[0-9.]+(?:[eE]-?[0-9]+)?)', value or "")
    return float(m.group(1)) if m else None


def analyze(rows):
    spans = []
    for r in rows:
        m = r.get("metadata") or {}
        a = {k[len("attributes."):]: v for k, v in m.items() if k.startswith("attributes.")}
        spans.append({**r, "a": a, "svc": m.get("resourceAttributes.service.name"),
                      "t0": ms(r["startTime"]), "t1": ms(r.get("endTime")) or ms(r["startTime"]) + secs(r.get("latency") or 0)})
    by_trace = collections.defaultdict(list)
    for x in spans:
        by_trace[x["traceId"]].append(x)
    skip = lambda sid: not sid or sid.startswith("lt-")

    # Brain requests: one trace per invoke_agent chat
    brain, model_errors = collections.defaultdict(list), 0
    for tid, T in by_trace.items():
        inv = next((x for x in T if x["svc"] == "chat" and x["name"] == "invoke_agent chat"), None)
        if not inv:
            continue
        gens = sorted((x for x in T if x["type"] == "GENERATION"), key=lambda x: x["t0"])
        sid = next((x["a"].get("ai.settings.context.trainertwin.session.id") for x in T
                    if x["a"].get("ai.settings.context.trainertwin.session.id")), None)
        if skip(sid):
            continue
        model_errors += sum(1 for g in gens if g.get("level") == "ERROR")
        tools = sorted((x for x in T if x["type"] == "TOOL"), key=lambda x: x["t0"])
        usage = [g.get("usageDetails") or {} for g in gens]
        brain[sid].append({
            "sid": sid, "t0": inv["t0"], "t1": inv["t1"], "seq": inv["a"].get("agent.turn.sequence"),
            "gens": gens, "tools": [x["name"] for x in tools],
            "tool_ms": sum(secs(x["a"].get("gen_ai.execute_tool.duration") or x.get("latency")) or 0 for x in tools),
            "in": inv["a"].get("gen_ai.usage.input_tokens") or sum(u.get("input", 0) + u.get("input_cached_tokens", 0) for u in usage),
            "cached": sum(g["a"].get("gen_ai.usage.cache_read.input_tokens") or 0 for g in gens),
            "out": inv["a"].get("gen_ai.usage.output_tokens") or sum(u.get("output", 0) for u in usage),
            "model": gens[-1]["a"].get("gen_ai.response.model") or gens[-1]["name"].replace("chat ", "") if gens else None,
            "used": False,
        })
    for L in brain.values():
        L.sort(key=lambda b: b["t0"])

    def brain_fields(b, ref_t0):
        """Brain timing measured from ref_t0 (voice: the agent's llm_request start; brain only: invoke start)."""
        if not b or not b["gens"]:
            return {"steps": 0, "tools": [], "in": None, "cached": None, "out": None, "model_name": None,
                    "prep": None, "hop": None, "retrieval": None, "pre_speak": None, "gen_ms": None, "decision": None, "tool_ms": None}
        g0, gl = b["gens"][0], b["gens"][-1]
        return {"steps": len(b["gens"]), "tools": b["tools"], "in": b["in"], "cached": b["cached"], "out": b["out"],
                "model_name": b["model"],
                "prep": g0["t0"] - ref_t0,                  # → first model call starts (hand-off + context retrieval)
                "hop": b["t0"] - ref_t0,                     # voice agent → brain picks the turn up (network + Vercel queue)
                "retrieval": g0["t0"] - b["t0"],             # brain: Eve + Qwen + vector DB, until the model starts
                "pre_speak": gl["t0"] - g0["t0"],           # earlier model calls + tools before the call that answered
                "gen_ms": gl["t1"] - gl["t0"],
                "decision": (g0["t1"] - g0["t0"]) if len(b["gens"]) > 1 else None,
                "tool_ms": b["tool_ms"] or None}

    def category(b, opening):
        if opening:
            return "opening"
        if b and any(t in TOOLS_FILE for t in b["tools"]):
            return "file"
        if b and b["tools"]:
            return "tool"
        if b and len(b["gens"]) > 1:
            return "restarted"
        return "plain"

    # Voice calls: one trace per job_entrypoint (one call = one trainer session)
    turns, sessions = [], []
    for tid, T in by_trace.items():
        job = next((x for x in T if x["name"] == "job_entrypoint" and x["svc"] == VOICE_SERVICE), None)
        if not job:
            continue
        sid = job["a"].get("trainer.session_id") or job.get("sessionId")
        if skip(sid):
            continue
        byid = {x["id"]: x for x in T}

        def ancestor(x, name):
            while x and x["name"] != name:
                x = byid.get(x.get("parentObservationId"))
            return x

        llm = collections.defaultdict(list)
        tts = collections.defaultdict(list)
        for x in T:
            at = ancestor(x, "agent_turn") if x["name"] in ("llm_request", "tts_request") else None
            if at:
                (llm if x["name"] == "llm_request" else tts)[at["id"]].append(x)
        speaking = {x["parentObservationId"]: x for x in T if x["name"] == "agent_speaking"}
        agent_turns = sorted((x for x in T if x["name"] == "agent_turn"), key=lambda x: x["t0"])
        user_ends = sorted(x["t1"] for x in T if x["name"] == "user_turn")
        S = []
        for i, at in enumerate(agent_turns):
            reqs = sorted(llm.get(at["id"], []), key=lambda x: x["t0"])
            if not reqs:
                continue                                    # a fixed line (goodbye) with no model call
            req = reqs[-1]
            ttft = secs(req["a"].get("gen_ai.response.time_to_first_chunk") or metric(req["a"].get("lk.llm_metrics"), "ttft"))
            first_tts = min(tts.get(at["id"], []), key=lambda x: x["t0"], default=None)
            tts_ms = secs(metric(first_tts["a"].get("lk.tts_metrics"), "ttfb")) if first_tts else None
            # The brain request this llm_request made: same session, invoke starts after the request went out
            # A cancelled request (learner spoke again) has no first chunk: only look inside its own lifetime,
            # or it would take the next turn's brain request.
            until = req["t0"] + ttft if ttft is not None else req["t1"]
            cands = [b for b in brain.get(sid, []) if not b["used"] and req["t0"] - 500 <= b["t0"] <= until]
            b = cands[-1] if cands else None
            for c in cands:
                c["used"] = True
            bf = brain_fields(b, req["t0"])
            opening = not S
            e2e = secs(at["a"].get("lk.e2e_latency"))
            sp = speaking.get(at["id"])
            interrupted = bool(at["a"].get("lk.interrupted"))
            if opening or e2e is not None:
                cat = category(b, opening)
            elif interrupted:
                cat = "cut"                                 # learner spoke again before the twin's first word
            else:
                last_user = max((e for e in user_ends if e <= at["t0"] + 500), default=None)
                cat = "nudge" if last_user is None or at["t0"] - last_user > 10000 else category(b, False)
            u = {"sid": sid, "type": "voice", "at": at["t0"], "end": (sp or at)["t1"], "cat": cat,
                 "spoken": e2e, "eot": secs(at["a"].get("lk.end_of_turn_delay")),
                 "brain_ttft": ttft, "tts": tts_ms, "interrupted": interrupted,
                 "speak_at": sp["t0"] if sp else None, "requests": max(1, len(cands)), "whole": (b["t1"] - b["t0"]) if b else None,
                 **bf}
            # Model to first word = brain first chunk minus everything before the answering model call started
            u["model"] = (ttft - u["prep"] - u["pre_speak"]) if (ttft is not None and u["prep"] is not None) else None
            S.append(u)
        turns += S
        op = S[0] if S else None
        start_spoken = (op["speak_at"] - job["t0"]) if (op and op["speak_at"]) else None
        sessions.append({
            "sid": sid, "type": "voice", "start": job["t0"], "duration": job["t1"] - job["t0"],
            "turns": len(S), "learner_turns": sum(1 for u in S if u["cat"] not in ("opening", "nudge", "cut")),
            "join": (op["at"] - job["t0"]) if op else None,
            "opening_prep": op["prep"] if op else None, "opening_hop": op["hop"] if op else None,
            "opening_retrieval": op["retrieval"] if op else None, "opening_model": op["model"] if op else None,
            "opening_tts": op["tts"] if op else None, "start_spoken": start_spoken,
            "worst": max((u["spoken"] for u in S if u["spoken"] is not None), default=None),
            "file_turns": sum(1 for u in S if u["cat"] == "file"),
            "interrupted": sum(1 for u in S if u["interrupted"]),
            "end_upload": next((x["latency"] for x in T if x["name"] == "session_end_upload"), None),
            "models": sorted({u["model_name"] for u in S if u["model_name"]}),
        })

    turns.sort(key=lambda u: u["at"])
    sessions.sort(key=lambda s: s["start"])
    voice_plain = [u for u in turns if u["type"] == "voice" and u["cat"] == "plain"]
    learner = [u for u in turns if u["cat"] not in ("opening", "nudge", "cut") and u["in"]]
    agg = {
        "window": [min((u["at"] for u in turns), default=0), max((u["end"] for u in turns), default=0)],
        "n_sessions": len(sessions), "n_turns": len(turns),
        "cats": dict(collections.Counter(u["cat"] for u in turns)),
        "models": sorted({u["model_name"] for u in turns if u["model_name"]}),
        "start_spoken": summ([s["start_spoken"] for s in sessions]),
        "join": summ([s["join"] for s in sessions]),
        "opening_ttft": summ([u["brain_ttft"] for u in turns if u["cat"] == "opening" and u["type"] == "voice"]),
        "plain_spoken": summ([u["spoken"] for u in voice_plain]),
        "plain_eot": summ([u["eot"] for u in voice_plain]),
        "plain_prep": summ([u["prep"] for u in voice_plain]),
        "plain_hop": summ([u["hop"] for u in voice_plain]),
        "plain_retrieval": summ([u["retrieval"] for u in voice_plain]),
        "plain_model": summ([u["model"] for u in voice_plain]),
        "plain_tts": summ([u["tts"] for u in voice_plain]),
        "cache": round(sum(u["cached"] or 0 for u in learner) / max(1, sum(u["in"] or 0 for u in learner)), 2),
        "in_tokens": summ([u["in"] for u in learner]),
        "file": summ([u["spoken"] for u in turns if u["cat"] == "file" and u["type"] == "voice"]),
        "tool_names": dict(collections.Counter(n for u in turns for n in u["tools"])),
        "interrupted": sum(1 for u in turns if u["interrupted"]),
        "dup_turns": sum(1 for u in turns if u["requests"] > 1),
        "model_errors": model_errors,
    }
    return {"agg": agg, "sessions": sessions, "turns": turns}


# ---------------------------------------------------------------- render

def s(v, digits=1):
    return "—" if v is None else f"{v / 1000:.{digits}f} s"


def when(v, fmt="%d %b %H:%M"):
    return dt.datetime.fromtimestamp(v / 1000, IST).strftime(fmt) if v else "—"


def esc(x):
    return html.escape(str(x))


def findings(a):
    out = []
    sp = a["start_spoken"]
    if sp["n"]:
        out.append(f"<b>Session start:</b> voice agent job starts → twin starts speaking, median <b>{s(sp['p50'])}</b> (p80 {s(sp['p80'])}). "
                   f"Joining the room and starting the session takes {s(a['join']['p50'])}; the brain's opening reply then takes {s(a['opening_ttft']['p50'])} "
                   f"to its first chunk. The learner's wait from clicking Start is longer: the time before the agent job starts is on the studio side, not in Langfuse.")
    pf = a["plain_spoken"]
    if pf["n"]:
        parts = [("end of speech → turn ends", a["plain_eot"]), ("hand-off to brain", a["plain_hop"]), ("context retrieval", a["plain_retrieval"]),
                 ("model to first word", a["plain_model"]), ("TTS first audio", a["plain_tts"])]
        big = max(parts, key=lambda p: p[1]["p50"] or 0)
        out.append(f"<b>Learner turns:</b> learner stops speaking → twin starts speaking, median <b>{s(pf['p50'])}</b> (p80 {s(pf['p80'])}, {pf['n']} turns). "
                   + ", ".join(f"{n} {s(x['p50'])}" for n, x in parts) + f". Biggest step: <b>{big[0]}</b>.")
    if a["in_tokens"]["n"]:
        out.append(f"<b>Prompt cache:</b> {round(100 * a['cache'])}% of input tokens were cached (median prompt {a['in_tokens']['p50']:,} tokens).")
    if a["file"]["n"]:
        out.append(f"<b>Turns that read a file</b> (read_document): {a['file']['n']}, median {s(a['file']['p50'])} to the first spoken word.")
    if a["dup_turns"]:
        out.append(f"<b>Repeated requests:</b> {a['dup_turns']} turn(s) reached the brain as 2+ requests (the voice agent re-sent the turn).")
    if a["interrupted"]:
        out.append(f"<b>Interrupted:</b> the learner talked over the twin on {a['interrupted']} turn(s).")
    if a["model_errors"]:
        out.append(f"<b>Model errors:</b> {a['model_errors']} model call(s) ended in ERROR on the brain.")
    return "".join(f"<li>{x}</li>" for x in out)


def model_label(model_id):
    """One name per model, whichever way the span spelled it (gateway id or provider's response model)."""
    m = (model_id or "").lower()
    for key, label in (("gemini-3.8-flash", "Gemini 3.8 Flash"), ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash"), ("gemma-4-26b", "Gemma 4 26B")):
        if key in m:
            return label
    return model_id.split("/")[-1] if model_id else None


def page_data(data, since):
    keep = ("sid", "type", "at", "cat", "spoken", "eot", "prep", "hop", "retrieval", "pre_speak", "model", "brain_ttft", "tts", "whole", "gen_ms",
            "decision", "tool_ms", "steps", "tools", "in", "cached", "out", "requests", "interrupted")
    sessions = [{**x, "model": " + ".join(sorted({model_label(m) for m in x["models"]})) or None} for x in data["sessions"]]
    return {"since": since, "sessions": sessions, "turns": [{k: u[k] for k in keep} for u in data["turns"]]}


def render(data, generated_at, window):
    a = data["agg"]
    w0, w1 = window
    payload = json.dumps(page_data(data, w0)).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Twin Response Monitoring</title>
<style>
:root {{ color-scheme: light; --surface:#fcfcfb; --panel:#ffffff; --line:#e6e5e0; --text:#0b0b0b; --text2:#52514e; --muted:#8c8b85;
  --c1:#2a78d6; --c2:#eb6834; --hi:#fff4e0; --hiText:#8a4b00; --bad:#c62828; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ color-scheme: dark; --surface:#1a1a19; --panel:#222221; --line:#33332f;
  --text:#ffffff; --text2:#c3c2b7; --muted:#8a897f; --c1:#3987e5; --c2:#d95926; --hi:#3a2e18; --hiText:#ffc46b; --bad:#ff7a7a; }} }}
:root[data-theme="dark"] {{ color-scheme: dark; --surface:#1a1a19; --panel:#222221; --line:#33332f; --text:#ffffff; --text2:#c3c2b7; --muted:#8a897f;
  --c1:#3987e5; --c2:#d95926; --hi:#3a2e18; --hiText:#ffc46b; --bad:#ff7a7a; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--surface); color:var(--text); font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:28px 16px 60px; }}
h1 {{ font-size:24px; margin:0 0 4px; }} h2 {{ font-size:17px; margin:34px 0 10px; }} h3 {{ font-size:14px; margin:0 0 8px; }}
.sub, .muted {{ color:var(--text2); }} .muted {{ font-size:12px; }}
.chips {{ display:flex; gap:8px; margin-top:16px; flex-wrap:wrap; align-items:center; }}
.chips button, .chips select {{ font:inherit; font-size:13px; padding:6px 14px; border-radius:999px; border:1px solid var(--line); background:var(--panel); color:var(--text); cursor:pointer; }}
.chips button[aria-pressed="true"] {{ background:var(--text); color:var(--surface); border-color:var(--text); }}
[hidden] {{ display:none !important; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); gap:12px; margin-top:16px; }}
.tile, .card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
.tlabel {{ color:var(--text2); font-size:13px; }} .tval {{ font-size:30px; font-weight:650; font-variant-numeric:tabular-nums; margin-top:2px; }}
.grid2 {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(min(440px, 100%), 1fr)); gap:12px; }}
.steps td.share {{ width:38%; }} .bar {{ height:8px; border-radius:4px; background:var(--c1); display:inline-block; vertical-align:middle; margin-right:8px; }}
.steps tr.big td {{ background:var(--hi); }} .steps tr.big td:first-child {{ color:var(--hiText); font-weight:600; }}
.steps tr.total td {{ font-weight:650; border-bottom:none; }}
.chart {{ width:100%; height:auto; display:block; }}
.chart .grid {{ stroke:var(--line); }} .chart .tick {{ fill:var(--muted); font-size:11px; }} .chart .lab {{ fill:var(--text); font-size:12.5px; font-weight:600; }}
.chart .p50 {{ stroke:var(--text); stroke-width:2; }} .chart .p50t {{ fill:var(--text2); font-size:11px; }}
.legend {{ display:flex; flex-wrap:wrap; gap:4px 14px; font-size:12px; color:var(--text2); margin-top:6px; }}
.legend i {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:5px; vertical-align:-1px; }}
.scroll {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th, td {{ text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }}
th {{ color:var(--text2); font-weight:600; font-size:12px; }} .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
td.sub1 {{ padding-left:26px; color:var(--text2); }}
.banner {{ margin-top:12px; padding:10px 14px; border-radius:8px; background:var(--hi); color:var(--hiText); font-size:13px; }}
.banner.ok {{ background:transparent; color:var(--text2); padding:0; }}
.tsub {{ font-size:13px; color:var(--text2); margin-top:2px; }}
code {{ font-size:12px; }} .bad {{ color:var(--bad); font-weight:600; }}
details > summary {{ cursor:pointer; font-size:17px; font-weight:650; margin:34px 0 10px; }}
ul.findings {{ padding-left:20px; margin:0; }} ul.findings li {{ margin:0 0 10px; }}
#tip {{ position:fixed; pointer-events:none; background:var(--text); color:var(--surface); font-size:12px; padding:6px 9px; border-radius:6px; max-width:380px; display:none; z-index:9; white-space:pre-line; }}
</style></head>
<body><main>
<h1>Twin Response Monitoring - Dashboard</h1>
<div class="sub">{esc(when(w0, '%d %b'))} → {esc(when(w1, '%d %b %Y'))} · Source: Langfuse</div>
<div class="chips" id="views">
  <button data-v="summary" aria-pressed="true">Summary</button>
  <button data-v="date" aria-pressed="false">By date</button>
  <select id="date" aria-label="Date" hidden></select>
  <select id="model" aria-label="Model"></select>
</div>
<div class="sub" id="counts" style="margin-top:8px"></div>
<div class="tiles" id="tiles"></div>

<div id="bydaywrap"><h2>By day</h2>
<div class="card scroll"><table id="byday"></table>
<div class="muted" style="margin-top:8px">Dates in IST, by when the session started. Pick a day under By date to see its turns.</div></div></div>

<h2>Where the time goes</h2>
<div class="grid2" id="steps"></div>
<div class="muted" style="margin-top:8px">p50 per step. Share = that step's part of all the time spent in this turn type, so shares add up to 100%. The highlighted row is the biggest step: optimize that first.</div>

<h2>Every turn</h2>
<div class="card"><div id="scatter"></div>
<div class="legend"><span><i style="background:var(--c1)"></i>Turn</span><span><i style="background:var(--c2)"></i>Learner talked over the twin</span><span>│ p50 line</span></div>
<div class="muted" style="margin-top:4px">One dot per turn. Hover a dot for its steps.</div></div>

<h2>Aggregates</h2>
<div class="card scroll"><table id="agg"></table></div>

<h2>Sessions</h2>
<div class="card scroll"><table id="sessions"></table>
<div class="muted" style="margin-top:8px">Turn p50 counts learner turns with no tool call. Length is the voice agent's job (join → leave).</div></div>

<details id="standout"><summary>What stands out (all dates)</summary>
<div class="card"><ul class="findings">{findings(a)}</ul></div></details>

<details><summary>All turns</summary><div class="card scroll"><table id="turns"></table>
<div class="muted" style="margin-top:8px">“×2” after steps = the voice agent sent this turn to the brain more than once.</div></div></details>

<details><summary>What this report can't see</summary>
<div class="card"><ul>
<li><b>Studio side:</b> Start click → session created, uploads, end-of-session webhook and the twin report are not in Langfuse. Session start here begins when the voice agent's job starts.</li>
<li><b>Screenshots sent with a message</b> go to the model as part of the turn, so they look like a normal turn. Only files read with <code>read_document</code> show up as file turns.</li>
<li><b>Before 10 Nov 2026 12:59 UTC</b> the voice agent didn't send spans to Langfuse, so earlier sessions are left out.</li>
<li><b>Who ran the session:</b> spans carry session IDs, not users.</li>
</ul></div></details>
</main><div id="tip"></div>
<script id="data" type="application/json">{payload}</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const TZ = {{ timeZone: 'Asia/Kolkata', hour12: false }};
const fmtS = (ms, d = 1) => ms == null ? '—' : (ms / 1000).toFixed(d) + ' s';
const when = (ms, sec) => ms ? new Date(ms).toLocaleString('en-GB', {{ ...TZ, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', ...(sec ? {{ second: '2-digit' }} : {{}}) }}) : '—';
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }})[c]);
const vals = (xs) => xs.filter((x) => x != null && !Number.isNaN(x));
function pct(xs, q) {{ xs = vals(xs).sort((a, b) => a - b); if (!xs.length) return null;
  const k = (xs.length - 1) * q, f = Math.floor(k), c = Math.min(f + 1, xs.length - 1); return Math.round(xs[f] + (xs[c] - xs[f]) * (k - f)); }}
const sum = (xs) => vals(xs).reduce((a, b) => a + b, 0);
const SINCE = when(D.since);
const catLabel = {{ plain: 'Learner turn', tool: 'Learner turn + tool', file: 'Reads a file (read_document)', restarted: 'Redone turn',
  opening: 'Opening', nudge: 'Nudge after silence', cut: 'Cut off before speaking' }};

const EOT = ['End of speech → turn ends', (r) => r.eot];
const HOP = ['Hand-off to brain', (r) => r.hop];
const RETR = ['Context retrieval', (r) => r.retrieval];
const MODEL = ['Model to first word', (r) => r.model];
const TTS = ['Text-to-speech first audio', (r) => r.tts];
function types(S, T, none) {{
  return {{ S, T, list: [
    {{ key: 'start', unit: 'sessions', title: '1 · Session start', note: 'Agent job starts → twin starts speaking', rows: S.filter((x) => x.start_spoken != null), total: (r) => r.start_spoken,
      steps: [['Agent joins room, starts session', (r) => r.join], ['Hand-off to brain', (r) => r.opening_hop], ['Context retrieval (loads the session)', (r) => r.opening_retrieval], ['Model to first word', (r) => r.opening_model], ['Text-to-speech first audio', (r) => r.opening_tts]] }},
    {{ key: 'plain', title: '2 · Learner turn', note: 'Learner stops speaking → twin starts speaking', rows: T.filter((x) => x.cat === 'plain' && x.spoken != null), total: (r) => r.spoken,
      steps: [EOT, HOP, RETR, MODEL, TTS] }},
    {{ key: 'tool', empty: none, title: '3 · Turn with a tool call', note: 'Learner stops speaking → twin starts speaking', rows: T.filter((x) => ['tool', 'file', 'restarted'].includes(x.cat) && x.spoken != null), total: (r) => r.spoken,
      steps: [EOT, HOP, RETR, ['Model call 1: decides on a tool', (r) => r.decision], ['Tool runs', (r) => r.tool_ms], ['Model call 2 to first word', (r) => r.model], TTS] }},
  ] }};
}}

function tiles(list) {{
  return list.map((t) => {{
    if (!t.rows.length && t.empty) return `<div class="tile"><div class="tlabel">${{esc(t.title)}} · first spoken word</div><div class="tval">None</div>
      <div class="tsub">${{esc(t.empty)}}</div></div>`;
    return `<div class="tile"><div class="tlabel">${{esc(t.title)}} · first spoken word</div><div class="tval">${{fmtS(pct(t.rows.map(t.total), .5))}}</div>
      <div class="tsub">p80 ${{fmtS(pct(t.rows.map(t.total), .8))}}</div></div>`;
  }}).join('');
}}

function stepTables(list) {{
  return list.map((t) => {{
    if (!t.rows.length) return `<div class="card"><h3>${{esc(t.title)}}</h3><div class="muted">${{esc(t.empty || 'No turns of this type in this window.')}}</div></div>`;
    const totalSum = sum(t.rows.map(t.total));
    const steps = t.steps.map(([name, get]) => {{
      const hit = t.rows.map(get).filter((v) => v != null && v > 0);
      const partial = hit.length < t.rows.length;
      return {{ name: partial ? `${{name}} · ${{hit.length}} of ${{t.rows.length}}` : name, p50: pct(hit, .5), share: sum(hit) }};
    }}).filter((x) => x.share > 0);
    const other = totalSum - sum(steps.map((x) => x.share));
    if (other > totalSum * 0.02) steps.push({{ name: 'Other / waiting (network, first sentence, queue)', p50: null, share: other }});
    const denom = sum(steps.map((x) => x.share)) || 1;
    const big = steps.reduce((m, x) => (x.share > m.share ? x : m), steps[0]);
    const rows = steps.map((x) => {{ const share = Math.round(100 * x.share / denom);
      return `<tr class="${{x === big ? 'big' : ''}}"><td>${{esc(x.name)}}</td><td class="num">${{fmtS(x.p50)}}</td>
        <td class="share"><span class="bar" style="width:${{Math.max(2, share)}}%"></span>${{share}}%</td></tr>`; }}).join('');
    return `<div class="card"><h3>${{esc(t.title)}} <span class="muted">· ${{t.rows.length}} ${{t.unit || 'turns'}}</span></h3>
      <table class="steps"><tr><th>Step</th><th class="num">p50</th><th>Share</th></tr>${{rows}}
      <tr class="total"><td>Total</td><td class="num">${{fmtS(pct(t.rows.map(t.total), .5))}}</td><td></td></tr></table></div>`;
  }}).join('');
}}

function scatter(list) {{
  const panels = list.filter((p) => p.rows.length);
  if (!panels.length) return '<div class="muted">No turns in this window.</div>';
  const vmax = Math.max(5000, ...panels.flatMap((p) => p.rows.map(p.total).filter((x) => x != null)));
  const top = Math.ceil(vmax / 5000) * 5000;
  const W = 760, left = 230, rowH = 64, H = panels.length * rowH + 28;
  const X = (ms) => left + (W - left - 14) * ms / top;
  let out = `<svg viewBox="0 0 ${{W}} ${{H}}" class="chart" role="img" aria-label="Time per turn, by turn type">`;
  for (let t = 0; t <= top; t += 5000) out += `<line x1="${{X(t)}}" x2="${{X(t)}}" y1="0" y2="${{H - 22}}" class="grid"/><text x="${{X(t)}}" y="${{H - 6}}" class="tick" text-anchor="${{t === top ? 'end' : 'middle'}}">${{t / 1000}} s</text>`;
  panels.forEach((p, i) => {{
    const y = i * rowH + 34, p50 = pct(p.rows.map(p.total), .5);
    out += `<text x="0" y="${{y + 4}}" class="lab">${{esc(p.title)}}</text>`;
    out += `<line x1="${{X(p50)}}" x2="${{X(p50)}}" y1="${{y - 18}}" y2="${{y + 18}}" class="p50"/><text x="${{X(p50)}}" y="${{y - 22}}" class="p50t" text-anchor="middle">p50 ${{fmtS(p50)}}</text>`;
    p.rows.forEach((r, j) => {{
      const v = p.total(r); if (v == null) return;
      const jitter = ((j * 3) % 7 - 3) * 4;
      const tip = `${{p.title}} · ${{(r.sid || '').slice(-8)}} · ${{when(r.at || r.start, true)}}\\nTotal ${{fmtS(v)}}\\n` +
        p.steps.map(([n, g]) => g(r) ? `${{n}}: ${{fmtS(g(r))}}` : null).filter(Boolean).join('\\n');
      out += `<circle cx="${{X(v)}}" cy="${{y + jitter}}" r="5" fill="${{r.interrupted ? 'var(--c2)' : 'var(--c1)'}}" stroke="var(--panel)" stroke-width="2" data-tip="${{esc(tip)}}"/>`;
    }});
  }});
  return out + '</svg>';
}}

function aggTable(list, S, T) {{
  const row = (label, xs, note = '', sub = false) => {{ xs = vals(xs);
    return xs.length ? `<tr><td class="${{sub ? 'sub1' : ''}}">${{esc(label)}}${{note ? `<div class="muted">${{esc(note)}}</div>` : ''}}</td><td class="num">${{xs.length}}</td>
      <td class="num"><b>${{fmtS(pct(xs, .5))}}</b></td><td class="num">${{fmtS(pct(xs, .8))}}</td><td class="num">${{fmtS(Math.min(...xs))}}</td><td class="num">${{fmtS(Math.max(...xs))}}</td></tr>`
      : `<tr><td class="${{sub ? 'sub1' : ''}}">${{esc(label)}}</td><td colspan="5" class="muted">none</td></tr>`; }};
  let h = '<tr><th>Metric</th><th class="num">n</th><th class="num">p50</th><th class="num">p80</th><th class="num">min</th><th class="num">max</th></tr>';
  for (const t of list) {{
    h += row(t.title, t.rows.map(t.total), t.note);
    for (const [n, g] of t.steps) h += row(n, t.rows.map(g), '', true);
  }}
  h += row('Brain time to first chunk (as the voice agent sees it)', T.filter((x) => x.cat !== 'opening').map((x) => x.brain_ttft), 'agent sends the turn → first text back');
  h += row('Nudge after silence', T.filter((x) => x.cat === 'nudge').map((x) => x.brain_ttft), 'brain time to first chunk; the learner was quiet 10 s+');
  const cut = T.filter((x) => x.cat === 'cut').length;
  if (cut) h += `<tr><td>Cut off before speaking<div class="muted">learner spoke again before the twin's first word; no spoken time</div></td><td class="num">${{cut}}</td><td colspan="4"></td></tr>`;
  h += row('Learner talked over the twin', T.filter((x) => x.interrupted).map((x) => x.spoken), 'interrupted turns, first spoken word');
  const L = T.filter((x) => x.in && x.cat !== 'opening');
  const cache = sum(L.map((x) => x.cached)) / Math.max(1, sum(L.map((x) => x.in)));
  h += `<tr><td>Prompt tokens cached<div class="muted">learner turns, all model calls</div></td><td class="num">${{L.length}}</td><td class="num"><b>${{Math.round(100 * cache)}}%</b></td><td colspan="3" class="muted">median prompt ${{(pct(L.map((x) => x.in), .5) || 0).toLocaleString()}} tokens</td></tr>`;
  return h;
}}

function sessionsTable(S, T) {{
  let h = '<tr><th>Session ID</th><th>Started (IST)</th><th class="num">Length</th><th class="num">Learner turns</th><th class="num">Start → first spoken word</th>' +
    '<th class="num">Turn p50</th><th class="num">Worst turn</th><th class="num">File turns</th><th class="num">Talked over</th><th>Model</th></tr>';
  for (const x of S) {{
    const plain = T.filter((u) => u.sid === x.sid && u.cat === 'plain').map((u) => u.spoken);
    h += `<tr><td><code>${{esc(x.sid)}}</code></td><td>${{when(x.start)}}</td><td class="num">${{fmtS(x.duration, 0)}}</td><td class="num">${{x.learner_turns}}</td>
      <td class="num">${{fmtS(x.start_spoken)}}</td><td class="num">${{fmtS(pct(plain, .5))}}</td><td class="num">${{fmtS(x.worst)}}</td>
      <td class="num">${{x.file_turns || ''}}</td><td class="num">${{x.interrupted || ''}}</td><td>${{esc(x.model || '—')}}</td></tr>`;
  }}
  return h;
}}

function turnsTable(T) {{
  let h = '<tr><th>Session</th><th>Time (IST)</th><th>Type</th><th class="num">First spoken word</th><th class="num">End of turn</th><th class="num">Brain first chunk</th>' +
    '<th class="num">Hand-off</th><th class="num">Context retrieval</th><th class="num">Model to first word</th><th class="num">TTS first audio</th>' +
    '<th class="num">Steps</th><th>Tools</th><th class="num">In tokens</th><th class="num">Cached</th><th class="num">Out tokens</th></tr>';
  for (const u of T) h += `<tr><td><code>${{esc(u.sid.slice(-8))}}</code></td><td>${{when(u.at, true)}}</td><td>${{esc(catLabel[u.cat] || u.cat)}}${{u.interrupted ? ' · talked over' : ''}}</td>
    <td class="num"><b>${{fmtS(u.spoken)}}</b></td><td class="num">${{fmtS(u.eot)}}</td><td class="num">${{fmtS(u.brain_ttft)}}</td><td class="num">${{fmtS(u.hop)}}</td><td class="num">${{fmtS(u.retrieval)}}</td>
    <td class="num">${{fmtS(u.model)}}</td><td class="num">${{fmtS(u.tts)}}</td>
    <td class="num">${{u.steps}}${{u.requests > 1 ? ' ×' + u.requests : ''}}</td><td>${{esc(u.tools.join(', '))}}</td>
    <td class="num">${{u.in == null ? '—' : u.in.toLocaleString()}}</td><td class="num">${{u.in ? Math.round(100 * u.cached / u.in) + '%' : '—'}}</td><td class="num">${{u.out ?? '—'}}</td></tr>`;
  return h;
}}

// Summary = every session; By date = sessions that started on one IST day (a session's turns stay with it).
const dayOf = (ms) => new Date(ms).toLocaleDateString('en-CA', TZ);
const dayLabel = (d) => new Date(d + 'T12:00:00+05:30').toLocaleDateString('en-GB', {{ ...TZ, weekday: 'short', day: '2-digit', month: 'short' }});
const sessionDay = Object.fromEntries(D.sessions.map((x) => [x.sid, dayOf(x.start)]));
const days = [...new Set(D.sessions.map((x) => sessionDay[x.sid]))].sort().reverse();

// Model filter: a session counts under the model that answered it (its turns go with it).
const sessionModel = Object.fromEntries(D.sessions.map((x) => [x.sid, x.model]));
const models = [...new Set(D.sessions.map((x) => x.model).filter(Boolean))].sort();
// Default: the model the latest call used (what prod runs now); a choice made in the filter is remembered.
const latest = [...D.sessions].reverse().find((x) => x.model);
let model = latest ? latest.model : 'all';
const byModel = (x) => model === 'all' || sessionModel[x.sid] === model;
const modelSel = document.getElementById('model');
modelSel.innerHTML = '<option value="all">All models</option>' + models.map((m) => `<option value="${{esc(m)}}">${{esc(m)}}</option>`).join('');

function byDayTable() {{
  let h = '<tr><th>Date</th><th class="num">Sessions</th><th class="num">Learner turns</th><th class="num">Session start p50</th>' +
    '<th class="num">Learner turn p50</th><th class="num">Learner turn p80</th><th class="num">Tool-call turns</th></tr>';
  for (const d of days) {{
    const S = D.sessions.filter((x) => sessionDay[x.sid] === d && byModel(x));
    const T = D.turns.filter((x) => sessionDay[x.sid] === d && byModel(x));
    const plain = T.filter((x) => x.cat === 'plain').map((x) => x.spoken);
    h += `<tr><td><a href="#" data-day="${{d}}">${{dayLabel(d)}}</a></td><td class="num">${{S.length}}</td><td class="num">${{sum(S.map((x) => x.learner_turns))}}</td>
      <td class="num">${{fmtS(pct(S.map((x) => x.start_spoken), .5))}}</td><td class="num"><b>${{fmtS(pct(plain, .5))}}</b></td><td class="num">${{fmtS(pct(plain, .8))}}</td>
      <td class="num">${{T.filter((x) => ['tool', 'file', 'restarted'].includes(x.cat)).length || ''}}</td></tr>`;
  }}
  return h;
}}

const dateSel = document.getElementById('date');
dateSel.innerHTML = days.map((d) => `<option value="${{d}}">${{dayLabel(d)}}</option>`).join('');
const viewBtns = [...document.querySelectorAll('#views button')];
function draw(view, day) {{
  const pick = (x) => (view === 'summary' || sessionDay[x.sid] === day) && byModel(x);
  const none = view === 'summary' ? `No tool calls since ${{SINCE}}` : `No tool calls on ${{dayLabel(day)}}`;
  const {{ S, T, list }} = types(D.sessions.filter(pick), D.turns.filter(pick), none);
  viewBtns.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === view)));
  dateSel.hidden = view !== 'date';
  document.getElementById('bydaywrap').hidden = view !== 'summary';
  document.getElementById('standout').hidden = view !== 'summary';
  document.getElementById('counts').textContent = `${{view === 'summary' ? 'All dates' : dayLabel(day)}} · ${{model === 'all' ? 'All models' : model}} · ${{S.length}} session${{S.length === 1 ? '' : 's'}} · ${{T.length}} turn${{T.length === 1 ? '' : 's'}}`;
  document.getElementById('byday').innerHTML = byDayTable();
  document.getElementById('tiles').innerHTML = tiles(list);
  document.getElementById('steps').innerHTML = stepTables(list);
  document.getElementById('scatter').innerHTML = scatter(list);
  document.getElementById('agg').innerHTML = aggTable(list, S, T);
  document.getElementById('sessions').innerHTML = sessionsTable(S, T);
  document.getElementById('turns').innerHTML = turnsTable(T);
}}
let view = 'summary', day = days[0];
try {{ const m = localStorage.getItem('latency-model'); if (m === 'all' || models.includes(m)) model = m; }} catch (e) {{}}
modelSel.value = model;
modelSel.addEventListener('change', () => {{ model = modelSel.value; try {{ localStorage.setItem('latency-model', model); }} catch (e) {{}} draw(view, day); }});
try {{ view = localStorage.getItem('latency-view') || 'summary'; day = days.includes(localStorage.getItem('latency-day')) ? localStorage.getItem('latency-day') : days[0]; }} catch (e) {{}}
const go = (v, d) => {{ view = v; day = d || day; dateSel.value = day;
  try {{ localStorage.setItem('latency-view', view); localStorage.setItem('latency-day', day); }} catch (e) {{}} draw(view, day); }};
viewBtns.forEach((b) => b.addEventListener('click', () => go(b.dataset.v)));
dateSel.addEventListener('change', () => go('date', dateSel.value));
document.getElementById('byday').addEventListener('click', (e) => {{ const a = e.target.closest('[data-day]'); if (a) {{ e.preventDefault(); go('date', a.dataset.day); }} }});
go(view, day);

const tip = document.getElementById('tip');
document.addEventListener('mousemove', (e) => {{ const t = e.target.closest('[data-tip]'); if (!t) {{ tip.style.display = 'none'; return; }}
  tip.textContent = t.dataset.tip; tip.style.display = 'block';
  tip.style.left = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8) + 'px'; tip.style.top = (e.clientY + 14) + 'px'; }});
</script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, help="only the last N days (default: everything since 10 Nov 2026 12:59 UTC)")
    ap.add_argument("--cached", action="store_true", help="re-render from logs/latency/langfuse-observations.json without calling Langfuse")
    ap.add_argument("--out", default=str(ROOT / "logs" / "latency-report.html"))
    args = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    raw_f = RAW / "langfuse-observations.json"
    if not args.cached:
        print(f"Pulling prod spans from Langfuse ({f'last {args.days:g} day(s)' if args.days else 'since 10 Nov 2026'})…", file=sys.stderr)
        pull(args.days, raw_f)
    raw = json.loads(raw_f.read_text())
    data = analyze(raw["rows"])
    generated = dt.datetime.now(IST).strftime("%d %b %H:%M IST")
    (RAW / "report-data.json").write_text(json.dumps(data, indent=1, default=str))
    Path(args.out).write_text(render(data, generated, (ms(raw["from"]), ms(raw["to"]))))
    a = data["agg"]
    print(f"Wrote {args.out}  ({a['n_sessions']} sessions, {a['n_turns']} turns)", file=sys.stderr)


if __name__ == "__main__":
    main()
