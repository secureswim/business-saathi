/* The operator console.
 *
 * Exists to make a deliberately opaque product explainable. For any query it
 * must answer: what was heard, what it meant, what data was used and at what
 * tier, which peers, what Cognee returned, what was computed, what went to the
 * model, whether it passed the validator, what n8n did, and what changed in the
 * graph.
 *
 * Every animation here maps to a real websocket event. The only timing that is
 * not a real event is the 200ms stagger on evidence cards, which is a render
 * delay on real results, not an invented one.
 */
const $ = (id) => document.getElementById(id);
const rs = (n) => "Rs " + Math.round(n).toLocaleString("en-IN");
const esc = (s) => String(s ?? "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

const STAGES = ["query_started", "stt_complete", "intent_detected", "context_loaded",
  "tool_started", "graph_retrieval", "evidence_complete", "reasoning_started",
  "validation_result", "response_ready", "tts_started", "learning_complete"];

let peers = [], extended = [], me = null, t0 = 0, evQueue = [], draining = false;
let activeOrchestrator = "state_machine";

/* ---------------- boot ---------------- */
(async function boot() {
  const cfg = await (await fetch("/api/config")).json();
  activeOrchestrator = cfg.adapters.orchestrator;
  $("adapters").innerHTML = Object.entries(cfg.adapters)
    .map(([k, v]) => `<span class="chip ${v === "simulated" ? "sim" : ""}">${k}: ${v}</span>`)
    .join("");
  renderPipeline();

  const data = await (await fetch("/api/merchants")).json();
  const sel = $("merchant");
  data.merchants.forEach(m => {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = `${m.id} · ${m.name} · ${m.locality}`
      + (m.has_obligations ? " · $" : "") + (m.has_stock_feed ? " · stock" : "");
    sel.appendChild(o);
  });
  sel.value = data.demo_merchant;
  loadGraph(sel.value);
  sel.onchange = () => loadGraph(sel.value);
  connect();
})();

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(`${proto}://${location.host}/ws`);
  sock.onopen = () => $("live").style.background = "var(--good)";
  sock.onclose = () => { $("live").style.background = "var(--warn)"; setTimeout(connect, 1200); };
  sock.onmessage = (e) => handle(JSON.parse(e.data));
}

/* ---------------- pipeline column ---------------- */
function renderPipeline(active = {}, failed = {}) {
  $("pipeline").innerHTML = STAGES.map(s =>
    `<div class="stage ${failed[s] ? "bad" : (active[s] !== undefined ? "on" : "")}">
       <span>${s.replace(/_/g, " ")}</span>
       <b>${active[s] !== undefined ? active[s] + "ms" : ""}</b></div>`).join("");
}
let stageTimes = {}, stageFailed = {};

function markStage(type, failed) {
  if (!STAGES.includes(type)) return;
  stageTimes[type] = t0 ? Date.now() - t0 : 0;
  if (failed) stageFailed[type] = true;
  renderPipeline(stageTimes, stageFailed);
}

/* ---------------- events ---------------- */
function handle(m) {
  markStage(m.type, m.type === "validation_result" && !m.passed);

  switch (m.type) {
    case "query_started":
      t0 = Date.now(); stageTimes = {}; stageFailed = {};
      renderPipeline();
      $("question").textContent = m.text;
      $("question").style.color = "var(--text)";
      $("answer").style.display = "none";
      $("evidence").innerHTML = "";
      $("heard").innerHTML = `<div class="tip">${esc(m.source)} input</div>`;
      evQueue = [];
      break;

    case "stt_complete":
      $("heard").innerHTML = `
        <div class="kv"><b>transcript</b><span>${esc(m.transcript)}</span>
          <b>engine</b><span>${esc(m.engine)}</span>
          <b>confidence</b><span>${m.confidence ?? "—"}</span>
          <b>latency</b><span>${m.ms ?? "—"}ms</span></div>`;
      break;

    case "intent_detected":
      $("intent").innerHTML = `
        <div class="kv"><b>intent</b><span class="hi">${m.intent}</span>
          <b>method</b><span>${m.method}</span>
          <b>plan</b><span>${esc(m.matched || "—")}</span>
          <b>memory</b><span>${m.memory_turns || 0} prior turn(s)</span>
          <b>review</b><span>${m.review_after_tools ? "enabled" : "not needed"}</span></div>
        <div class="tip" style="margin-top:6px">normalised: ${esc(m.normalised)}</div>
        <div class="tip">tools: ${(m.toolset || []).join(", ") || "none"}</div>`;
      break;

    case "context_loaded": {
      const c = m.merchant;
      $("mctx").innerHTML = `<div class="kv">
        <b>id</b><span class="hi">${c.id}</span>
        <b>category</b><span>${c.category}</span>
        <b>locality</b><span>${c.locality}</span>
        <b>band</b><span>${c.volume_band}</span>
        <b>history</b><span>${c.days_of_history}d</span>
        <b>obligations</b><span class="${c.has_obligations ? "hi" : "lo"}">${c.has_obligations ? "tier B" : "none"}</span>
        <b>stock feed</b><span class="${c.has_stock_feed ? "hi" : "lo"}">${c.has_stock_feed ? "tier B" : "none"}</span>
      </div>`;
      break;
    }

    case "tool_result":
      evQueue.push(m.evidence);
      drain();
      break;

    case "response_ready": {
      $("answer").style.display = "block";
      $("answer").innerHTML = `${esc(m.english)}
        <div style="color:var(--muted);margin-top:7px">${esc(m.hinglish)}</div>
        <div class="tip" style="margin-top:7px">evidence: ${(m.evidence_refs || []).map(esc).join(", ") || "none"}</div>
        ${(m.limitations || []).length ? `<div class="tip lo">limits: ${m.limitations.map(esc).join(" · ")}</div>` : ""}
        <div class="label" style="margin-top:7px">${m.intent} · ${m.confidence || "—"} confidence · ${m.elapsed_ms}ms · ${esc(m.reasoner)}</div>`;
      break;
    }

    case "validation_result":
      if (!m.passed || m.substituted) {
        $("answer").classList.add("bad");
        $("answer").insertAdjacentHTML("beforeend",
          `<div class="label er" style="margin-top:6px">grounding failed — unbacked ${
            (m.unbacked || []).join(", ")} — template answer substituted</div>`);
      } else {
        $("answer").classList.remove("bad");
      }
      break;

    case "action_proposed":
    case "workflow_started":
    case "outcome_measured":
      renderRunFromEvent(m);
      break;

    case "workflow_node":
      if (currentRun && m.node === "campaign running" && m.status === "success") {
        currentRun.state = "running";
        drawRun();
      }
      addNode(m);
      break;

    case "graph_writeback":
      addNode({ node: "graph writeback", status: m.degraded ? "degraded" : "success",
                detail: `${m.detail || 'written'} · ${m.store}` });
      flashNode();
      break;

    case "learning_complete":
      if (currentRun) {
        currentRun.state = "learned";
        drawRun();
      }
      showCounters(m.after, m.before);
      loadGraph($("merchant").value);
      break;

    case "proactive_alert":
      renderAlert(m);
      break;

    case "context_stored":
      addNode({ node: "merchant context stored", status: "success",
                detail: `${m.fact.kind}: ${esc(m.fact.utterance)}` });
      break;

    case "reset":
      location.reload();
      break;
  }
}

/* ---------------- evidence cards, staggered ---------------- */
function drain() {
  if (draining) return;
  draining = true;
  const step = () => {
    const ev = evQueue.shift();
    if (!ev) { draining = false; return; }
    renderEvidence(ev);
    setTimeout(step, 200);
  };
  step();
}

function renderEvidence(ev) {
  const card = document.createElement("div");
  card.className = "ev" + (ev.available ? "" : " off");
  card.innerHTML = `
    <div class="top">
      <span class="label">${ev.tool}</span>
      <span class="row" style="gap:4px">
        <span class="src ${ev.source}">${ev.source.replace(/_/g, " ")}</span>
        <span class="tier">tier ${ev.tier}</span>
      </span>
    </div>
    ${ev.available ? summary(ev) : `<div class="tip">${esc(ev.value.reason)}</div>`}
    ${ev.ask ? `<div class="tip lo" style="margin-top:6px">asks the merchant for: ${esc(ev.ask)} (${esc(ev.ask_subject || "")})</div>` : ""}
    <details><summary>basis</summary><pre>${esc(JSON.stringify(ev.basis, null, 1))}</pre></details>`;
  $("evidence").appendChild(card);

  if (ev.tool === "get_peer_cohort" && ev.ops_value) {
    peers = (ev.basis.peers || []); extended = (ev.basis.extended || []);
    me = ev.ops_value.merchant; layout();
    showCounters({ cohort_size: ev.ops_value.cohort_size, tried: null, worked: null,
                   success_rate: null }, null, true);
  }
  if (ev.tool === "get_peer_playbook" && ev.value.best) {
    showCounters({ cohort_size: ev.value.cohort_size, tried: ev.value.best.tried,
                   worked: ev.value.best.worked,
                   success_rate: ev.value.best.success_rate });
  }
}

function summary(ev) {
  const v = ev.value, kv = [];
  const add = (k, val, cls = "") => kv.push(`<b>${k}</b><span class="${cls}">${val}</span>`);
  switch (ev.tool) {
    case "get_merchant_context":
      add("category", v.category); add("locality", v.locality);
      add("history", v.days_of_history + "d"); break;
    case "get_business_health":
      add("headline", v.headline.replace(/_/g, " "), "hi");
      add("change", v.change_pct + "%", v.change_pct < 0 ? "lo" : "hi");
      add("daily", rs(v.current_daily)); add("avg ticket", v.avg_ticket ? rs(v.avg_ticket) : "—");
      add("volatility", v.volatility_pct + "%"); break;
    case "get_sales_trend":
      add("change", v.change_pct + "%", v.change_pct < 0 ? "lo" : "hi");
      add("now / baseline", `${rs(v.current_daily)} / ${rs(v.baseline_daily)}`);
      add("worst band", `${v.worst_band} (${v.worst_band_pct}%)`, "lo"); break;
    case "get_time_patterns":
      add("peaks", (v.peak_bands || []).join(", "), "hi");
      add("best day", v.best_weekday || "—");
      add("weekend", v.weekend_ratio ? v.weekend_ratio + "x" : "—"); break;
    case "get_peer_cohort":
      add("cohort", v.cohort_size, "hi"); add("extended ring", v.extended_size);
      add("privacy floor", v.sufficient ? "met" : "NOT MET", v.sufficient ? "" : "er"); break;
    case "get_peer_relative_anomaly":
      add("you", v.your_change_pct + "%", "lo");
      add("peer median", v.peer_median_change_pct + "%");
      add("gap", v.gap_pct + " pp"); add("verdict", v.verdict.replace(/_/g, " "), "hi"); break;
    case "get_peer_playbook":
      if (!v.best) { add("options", "none"); break; }
      add("situation", v.situation_kind);
      add("action", v.best.action.replace(/_/g, " "), "hi");
      add("worked", `${v.best.worked} of ${v.best.tried} (${v.best.success_rate}%)`, "hi");
      add("median change", v.best.median_delta + "%"); break;
    case "get_failed_plays":
      (v.buckets || []).forEach(b =>
        add(b.range, `${b.failed}/${b.tried} failed · median ${b.median_delta}%`,
            b.failure_rate > 50 ? "lo" : "hi"));
      add("safest", v.safest_bucket.range, "hi"); break;
    case "get_local_pattern":
      add("cohort", v.cohort_size); add("cohort change", v.cohort_change_pct + "%");
      add("busiest", (v.busiest_bands || []).join(", ")); break;
    case "get_cohort_seasonality":
      add("cohort", v.cohort_size);
      add("festive near", v.festive_window_near ? "yes" : "no"); break;
    case "get_cohort_profile":
      add("peaks", (v.peak_bands || []).join(", "), "hi");
      add("daily range", `${rs(v.expected_daily_low)} – ${rs(v.expected_daily_high)}`);
      add("aggregated from", v.cohort_size + " merchants"); break;
    case "get_demand_forecast":
      add("expected", rs(v.total_expected), "hi");
      add("band", `${rs(v.total_low)} – ${rs(v.total_high)}`);
      add("busiest", v.busiest_day || "—");
      if (v.next_rush) add("next rush", `${v.next_rush.weekday} ${v.next_rush.hour}:00 · ${v.next_rush.multiple}x`);
      break;
    case "get_money_position":
      add("scope", v.scope.replace(/_/g, " "), v.scope === "inflow_only" ? "lo" : "hi");
      add("expected in", rs(v.revenue_expected));
      if (v.scope === "net_position") {
        add("obligations", rs(v.obligations_due), "lo");
        add("net", rs(v.net_expected));
      } else {
        add("obligations", "not visible", "lo");
      }
      if (v.verdict) add("verdict", v.verdict, v.verdict === "unsafe" ? "er" : "hi");
      break;
    case "get_optional_stock_context":
      add("subject", v.subject); add("quantity", `${v.quantity} ${v.unit || ""}`, "hi");
      add("source", v.source.replace(/_/g, " ")); break;
    case "get_action_history":
      (v.actions || []).forEach(a =>
        add(a.type.replace(/_/g, " "), `${a.verdict || "running"} ${a.delta_pct ?? ""}`));
      break;
    case "get_recent_situations":
      (v.situations || []).forEach(s => add(s.kind, `${s.severity}% · ${s.detected_on}`));
      break;
    case "propose_action":
      add("type", v.type.replace(/_/g, " "), "hi");
      add("params", esc(JSON.stringify(v.params)));
      add("evidence", esc(v.evidence_summary)); break;
    default:
      add("value", esc(JSON.stringify(v).slice(0, 140)));
  }
  return `<div class="kv">${kv.join("")}</div>`;
}

/* ---------------- counters ---------------- */
function showCounters(after, before, partial) {
  $("c1").textContent = after.cohort_size ?? "—";
  if (!partial) {
    $("c2").textContent = after.tried ?? "—";
    $("c3").textContent = after.worked ?? "—";
    $("c4").textContent = after.success_rate == null ? "—" : after.success_rate + "%";
  }
  if (before && before.worked !== after.worked) {
    ["c3box", "c4box"].forEach(id => {
      $(id).classList.add("changed");
      setTimeout(() => $(id).classList.remove("changed"), 1600);
    });
  }
}

async function loadGraph(id) {
  try {
    const g = await (await fetch(`/api/graph/${id}`)).json();
    peers = g.peers || []; extended = g.extended || []; me = g.merchant;
    layout();
    $("patterns").innerHTML = (g.learned_patterns || []).slice(0, 6).map(p =>
      `<div class="node"><span>${p.situation_kind} → ${p.action_type.replace(/_/g, " ")}</span>
       <span class="num">${p.worked}/${p.tried}</span></div>`).join("")
      || `<div class="tip">no learned patterns for this cohort yet</div>`;
    const c = await (await fetch(`/api/cohort/${id}`)).json();
    showCounters(c);
  } catch (e) { /* the canvas simply stays as it was */ }
}

/* ---------------- cohort canvas ---------------- */
const cv = $("graph"), cx = cv.getContext("2d");
let nodes = [], tt = 0;

function layout() {
  const all = peers.map(p => ({ ...p, ring: "tight" }))
    .concat(extended.map(p => ({ ...p, ring: "ext" })));
  nodes = all.map((p, i) => ({
    ...p,
    a: (i / Math.max(1, all.length)) * Math.PI * 2 + (p.ring === "ext" ? 0.4 : 0),
    r: p.ring === "tight" ? 0.42 : 0.80,
    jitter: (i * 37 % 100) / 16,
  }));
}
function flashNode() {
  const n = nodes.find(x => !x.flash) || nodes[0];
  if (n) n.flash = 1;
}
function frame() {
  const w = cv.clientWidth, h = cv.clientHeight;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = h * dpr; cx.setTransform(dpr, 0, 0, dpr, 0, 0); }
  cx.clearRect(0, 0, w, h);
  tt += 0.005;
  const cxp = w / 2, cyp = h / 2, R = Math.min(w, h) / 2 - 20;

  nodes.forEach(n => {
    const a = n.a + tt * (n.ring === "tight" ? 0.5 : 0.25);
    n.x = cxp + Math.cos(a) * R * n.r + Math.sin(tt * 2 + n.jitter) * 3;
    n.y = cyp + Math.sin(a) * R * n.r + Math.cos(tt * 2 + n.jitter) * 3;
    cx.beginPath(); cx.moveTo(cxp, cyp); cx.lineTo(n.x, n.y);
    const s = n.similarity || 0.7;
    cx.strokeStyle = `rgba(0,186,242,${Math.max(0.05, (s - 0.65) * 1.5)})`;
    cx.lineWidth = n.ring === "tight" ? 1.1 : 0.5;
    cx.stroke();
  });
  nodes.forEach(n => {
    cx.beginPath(); cx.arc(n.x, n.y, n.flash ? 8 : 4.5, 0, 6.2832);
    if (n.flash) {
      cx.fillStyle = "#00BAF2"; cx.fill();
      n.flash *= 0.99; if (n.flash < 0.02) n.flash = 0;
    } else {
      cx.strokeStyle = n.ring === "tight" ? "#5b6470" : "#33383f";
      cx.lineWidth = 1.2; cx.stroke();
    }
  });
  cx.beginPath(); cx.arc(cxp, cyp, 8, 0, 6.2832); cx.fillStyle = "#00BAF2"; cx.fill();
  if (me) {
    cx.font = "500 11px 'JetBrains Mono', monospace"; cx.fillStyle = "#F2F2F4";
    cx.fillText(me.id, cxp + 14, cyp + 1);
    cx.fillStyle = "#8A8A96"; cx.font = "10px 'JetBrains Mono', monospace";
    cx.fillText(`${me.category} · ${me.locality}`, cxp + 14, cyp + 14);
  }
  requestAnimationFrame(frame);
}
frame();

cv.onmousemove = (e) => {
  const r = cv.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  const hit = nodes.find(n => Math.hypot(n.x - mx, n.y - my) < 10);
  $("hover").innerHTML = hit
    ? `<span class="num hi">${hit.id}</span> similarity <span class="num">${hit.similarity}</span> · `
      + Object.entries(hit.components || {}).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · ")
    : "";
};

/* ---------------- workflow column ---------------- */
const ORDER = ["proposed", "validating", "running", "measuring", "learned"];
let currentRun = null;

function renderRunFromEvent(m) {
  if (m.type === "action_proposed") {
    currentRun = { run_id: m.run_id, type: m.type_ || m.type, state: "proposed",
                   params: m.params, evidence_summary: m.evidence_summary,
                   guardrails_passed: m.guardrails_passed, revised: m.revised,
                   reason: m.reason };
    currentRun.type = m.type === "action_proposed" ? (m.params && "evening_offer") : m.type;
    $("nodes").innerHTML = "";
  }
  if (m.type === "workflow_started" && currentRun) currentRun.state = "validating";
  if (m.type === "outcome_measured" && currentRun) {
    currentRun.state = "measuring"; currentRun.outcome = m;
  }
  drawRun();
}

function drawRun() {
  if (!currentRun) return;
  const idx = ORDER.indexOf(currentRun.state);
  const steps = ORDER.map((s, i) => `<span class="step ${i <= idx ? "on" : ""}">${s}</span>`).join("");
  const o = currentRun.outcome;
  $("run").innerHTML = `
    <div class="kv">
      <b>run</b><span>${currentRun.run_id}</span>
      <b>params</b><span>${esc(JSON.stringify(currentRun.params))}</span>
      <b>evidence</b><span>${esc(currentRun.evidence_summary || "")}</span>
      <b>guardrails</b><span class="${currentRun.guardrails_passed ? "hi" : "er"}">${
        currentRun.guardrails_passed ? "passed" : "violation"}</span>
    </div>
    <div class="steps">${steps}</div>
    ${currentRun.reason ? `<div class="tip lo">${esc(currentRun.reason)}</div>` : ""}
    ${o ? `<div class="kv"><b>outcome</b><span class="hi">${o.verdict} ${
      o.delta_pct > 0 ? "+" : ""}${o.delta_pct}%</span>
      <b>simulated</b><span class="lo">${o.simulated ? "yes" : "no"}</span>
      ${o.forced ? '<b>forced</b><span class="er">presenter override</span>' : ""}</div>` : ""}
    <div class="row">
      ${currentRun.state === "proposed"
        ? `<button class="primary" onclick="act('${currentRun.run_id}','approve')">Approve</button>
           <button onclick="act('${currentRun.run_id}','reject')">Reject</button>` : ""}
      ${currentRun.state === "running" && activeOrchestrator !== "n8n"
        ? `<button class="primary" onclick="measure('${currentRun.run_id}')">Fast-forward</button>
           <button onclick="measure('${currentRun.run_id}','recovered')">Force recovered</button>` : ""}
    </div>`;
}

function addNode(m) {
  if ($("nodes").firstChild && $("nodes").firstChild.className === "tip") $("nodes").innerHTML = "";
  const cls = m.status === "failed" ? "er" : (m.status === "degraded" ? "lo" : "hi");
  $("nodes").insertAdjacentHTML("beforeend",
    `<div class="node"><span>${esc(m.node)}${m.simulated ? " (simulated)" : ""}</span>
     <span class="${cls}">${esc(m.status)}</span></div>
     ${m.detail ? `<div class="tip">${esc(m.detail)}</div>` : ""}`);
}

function renderAlert(a) {
  if ($("alerts").firstChild && $("alerts").firstChild.className === "tip") $("alerts").innerHTML = "";
  const conds = Object.entries(a.conditions || {}).map(([k, c]) =>
    `<div class="cond ${c.ok ? "ok" : "no"}"><span>${k.replace(/_/g, " ")}</span>
     <span>${esc(String(c.value))}</span></div>`).join("");
  $("alerts").insertAdjacentHTML("afterbegin",
    `<div class="alert">
       <div class="label lo">proactive · ${a.merchant_id} · ${a.alert_type}</div>
       <div style="margin-top:6px">${esc(a.hinglish)}</div>
       <div class="tip" style="margin-top:6px">${esc(a.english)}</div>
       <div style="margin-top:8px">${conds}</div>
     </div>`);
}

/* ---------------- controls ---------------- */
async function act(id, what) {
  const r = await fetch(`/api/action/${id}/${what}`, { method: "POST" }).then(r => r.json());
  if (currentRun && r.state) { currentRun.state = r.state; drawRun(); }
  if (r.revised) { currentRun.reason = r.reason; currentRun.guardrails_passed = false; drawRun(); }
}
async function measure(id, force) {
  const url = `/api/admin/measure?run_id=${id}` + (force ? `&force=${force}` : "");
  const r = await fetch(url, { method: "POST" }).then(r => r.json());
  if (currentRun) { currentRun.state = r.state || "learned"; drawRun(); }
}
window.act = act; window.measure = measure;

$("send").onclick = send;
$("q").onkeydown = (e) => { if (e.key === "Enter") send(); };
async function send() {
  const text = $("q").value.trim(); if (!text) return;
  $("q").value = "";
  await fetch("/api/query", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ merchant_id: $("merchant").value, text })
  });
}
$("scan").onclick = () => fetch("/api/admin/trigger-alert", { method: "POST" });
$("day").onclick = () => fetch("/api/admin/simulate-day", { method: "POST" });
$("reset").onclick = () => fetch("/api/admin/reset", { method: "POST" });
