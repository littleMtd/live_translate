from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.replay_phase0_stt_candidates import verify_audio_asset
from scripts.routing_span_annotations import (
    ROUTING_ACTIONS,
    SOURCE_CLASSES,
    RoutingAnnotationStore,
    build_routing_tasks,
    load_manifest,
)


DEFAULT_MANIFEST = PROJECT_ROOT / "scratch" / "analysis" / "phase0_replay_manifest_20260624.json"
DEFAULT_ANNOTATIONS = PROJECT_ROOT / "scratch" / "analysis" / "phase0_routing_spans_20260624.annotations.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Routing Span Review</title>
<style>
:root { --ink:#172033; --muted:#667085; --line:#d7ddea; --soft:#f6f8fb; --blue:#2359a8; --green:#137a48; --red:#b42318; --amber:#a15c00; }
* { box-sizing:border-box; }
body { margin:0; color:var(--ink); background:#fff; font:14px/1.45 system-ui,Segoe UI,sans-serif; }
button,input,select,textarea { font:inherit; }
button { border:1px solid var(--line); background:#fff; border-radius:6px; padding:7px 10px; cursor:pointer; }
button:hover { border-color:#98a2b3; }
button.primary { background:var(--blue); border-color:var(--blue); color:#fff; }
button.complete { background:var(--green); border-color:var(--green); color:#fff; }
.app { display:grid; grid-template-columns:220px minmax(0,1fr); min-height:100vh; }
aside { border-right:1px solid var(--line); background:var(--soft); padding:16px 12px; position:sticky; top:0; height:100vh; overflow:auto; }
h1 { font-size:17px; margin:0 0 4px; }
.summary { color:var(--muted); margin-bottom:12px; }
.case-list { display:grid; gap:5px; }
.case-button { display:flex; justify-content:space-between; width:100%; text-align:left; }
.case-button.active { border-color:var(--blue); color:var(--blue); font-weight:650; }
.status { font-size:12px; color:var(--muted); }
.status.complete { color:var(--green); }
main { padding:20px clamp(16px,3vw,40px) 60px; max-width:1500px; width:100%; }
.topbar { display:flex; justify-content:space-between; align-items:flex-start; gap:20px; margin-bottom:16px; }
.meta { color:var(--muted); }
.actions { display:flex; gap:8px; }
.text-grid { display:grid; grid-template-columns:1fr 1fr; border:1px solid var(--line); border-radius:8px; overflow:hidden; margin-bottom:18px; }
.text-panel { padding:14px; min-height:110px; }
.text-panel + .text-panel { border-left:1px solid var(--line); }
.label { color:var(--muted); font-size:12px; text-transform:uppercase; margin-bottom:6px; }
.asset { border-top:1px solid var(--line); padding:18px 0 20px; }
.asset-head { display:flex; justify-content:space-between; gap:12px; align-items:center; margin-bottom:10px; }
.asset-title { font-weight:700; }
.pills { display:flex; gap:6px; flex-wrap:wrap; }
.pill { border:1px solid var(--line); border-radius:999px; padding:3px 8px; color:var(--muted); }
audio { width:100%; margin:4px 0 8px; }
canvas { width:100%; height:90px; display:block; background:#f8fafc; border:1px solid var(--line); border-radius:6px; cursor:crosshair; }
.time-readout { margin:6px 0 10px; color:var(--muted); font-variant-numeric:tabular-nums; }
.span-form { display:grid; grid-template-columns:110px 110px 160px 160px minmax(140px,1fr) auto; gap:8px; align-items:end; }
.field { display:grid; gap:4px; }
.field span { color:var(--muted); font-size:12px; }
input,select,textarea { border:1px solid var(--line); border-radius:6px; padding:7px 8px; min-width:0; background:#fff; }
.mark-buttons { display:flex; gap:6px; margin:8px 0; }
.quick-labels { display:flex; gap:7px; flex-wrap:wrap; margin:8px 0 12px; }
.quick-labels button { border-left-width:5px; }
.selection-hint { color:var(--muted); margin:7px 0; }
.span-table { width:100%; border-collapse:collapse; margin-top:12px; }
.span-table th,.span-table td { border-bottom:1px solid #e7eaf0; padding:7px; text-align:left; vertical-align:top; }
.span-table th { color:var(--muted); font-size:12px; font-weight:600; }
.coverage { margin-top:8px; color:var(--muted); }
.coverage.warn { color:var(--amber); }
.notes { margin-top:18px; display:grid; gap:5px; }
.notes textarea { min-height:70px; resize:vertical; }
.message { min-height:20px; margin-top:10px; color:var(--green); }
.message.error { color:var(--red); }
@media (max-width:900px) {
  .app { grid-template-columns:1fr; }
  aside { position:static; height:auto; border-right:0; border-bottom:1px solid var(--line); }
  .case-list { grid-template-columns:repeat(3,minmax(0,1fr)); }
  .span-form { grid-template-columns:1fr 1fr; }
  .text-grid { grid-template-columns:1fr; }
  .text-panel + .text-panel { border-left:0; border-top:1px solid var(--line); }
}
</style>
</head>
<body>
<div class="app">
  <aside>
    <h1>Routing spans</h1>
    <div id="summary" class="summary"></div>
    <div id="caseList" class="case-list"></div>
  </aside>
  <main>
    <div class="topbar">
      <div><h1 id="caseTitle"></h1><div id="caseMeta" class="meta"></div></div>
      <div class="actions"><button id="saveDraft">Save draft</button><button id="markComplete" class="complete">Complete</button></div>
    </div>
    <div class="text-grid">
      <section class="text-panel"><div class="label">Source</div><div id="sourceText"></div></section>
      <section class="text-panel"><div class="label">Translation</div><div id="targetText"></div></section>
    </div>
    <div id="assets"></div>
    <label class="notes"><span>Case notes</span><textarea id="caseNotes"></textarea></label>
    <div id="message" class="message"></div>
  </main>
</div>
<script>
const state = { tasks:[], annotations:{}, sourceClasses:[], routingActions:[], currentId:"", waveforms:new Map(), selections:new Map(), saveTimer:null };
const colors = { host:"#2f6fed", content_other:"#12a36d", alert_tts:"#e5484d", mixed:"#8e4ec6", unrelated:"#98a2b3", uncertain:"#d97706" };
const defaultActions = { host:"translate", content_other:"translate", alert_tts:"suppress", mixed:"extract_host", unrelated:"suppress", uncertain:"exclude" };
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const task = () => state.tasks.find(item => item.sample_id === state.currentId);
function annotation(sampleId) {
  if (!state.annotations[sampleId]) state.annotations[sampleId] = { status:"draft", notes:"", assets:{} };
  const item = state.annotations[sampleId];
  if (!item.assets) item.assets = {};
  return item;
}
function assetAnnotation(utteranceId) {
  const item = annotation(state.currentId);
  if (!item.assets[utteranceId]) item.assets[utteranceId] = { spans:[], coverage_gaps:[] };
  return item.assets[utteranceId];
}
function renderSidebar() {
  const complete = state.tasks.filter(item => annotation(item.sample_id).status === "complete").length;
  $("summary").textContent = `${complete}/${state.tasks.length} complete`;
  $("caseList").innerHTML = "";
  state.tasks.forEach(item => {
    const status = annotation(item.sample_id).status;
    const button = document.createElement("button");
    button.className = "case-button" + (item.sample_id === state.currentId ? " active" : "");
    button.innerHTML = `<span>${esc(item.sample_id)}</span><span class="status ${status}">${esc(status)}</span>`;
    button.onclick = () => { state.currentId = item.sample_id; render(); };
    $("caseList").appendChild(button);
  });
}
function options(values, selected) { return values.map(value => `<option value="${esc(value)}" ${value===selected?"selected":""}>${esc(value)}</option>`).join(""); }
function render() {
  const item = task(); if (!item) return;
  renderSidebar();
  $("caseTitle").textContent = item.sample_id;
  $("caseMeta").textContent = `seq ${item.sequence_id} · ${item.audio_assets.length} WAV · ${item.speaker_source_tags.join(", ")}`;
  $("sourceText").textContent = item.source_text || "";
  $("targetText").textContent = item.target_text || "";
  $("caseNotes").value = annotation(item.sample_id).notes || "";
  $("message").textContent = "";
  $("assets").innerHTML = "";
  state.waveforms.clear();
  state.selections.clear();
  item.audio_assets.forEach((asset,index) => renderAsset(item, asset, index));
}
function renderAsset(item, asset, index) {
  const box = document.createElement("section"); box.className = "asset"; box.dataset.utteranceId = asset.utterance_id;
  const ann = assetAnnotation(asset.utterance_id); const spans = ann.spans || [];
  box.innerHTML = `
    <div class="asset-head"><div class="asset-title">${index+1}. ${esc(asset.utterance_id)}</div><div class="pills"><span class="pill">${esc(asset.chunk_role)}</span><span class="pill">${esc(asset.source_kind)}</span><span class="pill">${asset.duration_seconds.toFixed(2)}s</span></div></div>
    <audio controls preload="metadata" src="${esc(asset.audio_url)}"></audio>
    <canvas width="1200" height="90"></canvas>
    <div class="time-readout">current <strong>0.000s</strong></div>
    <div class="mark-buttons"><button data-mark="start">Set start @ current</button><button data-mark="end">Set end @ current</button><button data-full="1">Use full duration</button></div>
    <div class="selection-hint">Drag on waveform or set start/end, then apply one label:</div>
    <div class="quick-labels">
      <button data-paint="host|translate" style="border-left-color:${colors.host}">Host</button>
      <button data-paint="alert_tts|suppress" style="border-left-color:${colors.alert_tts}">Alert TTS</button>
      <button data-paint="content_other|translate" style="border-left-color:${colors.content_other}">Content</button>
      <button data-paint="mixed|extract_host" style="border-left-color:${colors.mixed}">Mixed: extract host</button>
      <button data-paint="mixed|extract_content" style="border-left-color:${colors.mixed}">Mixed: extract content</button>
      <button data-paint="unrelated|suppress" style="border-left-color:${colors.unrelated}">Unrelated</button>
      <button data-paint="uncertain|exclude" style="border-left-color:${colors.uncertain}">Uncertain</button>
    </div>
    <div class="span-form">
      <label class="field"><span>Start</span><input data-field="start" type="number" min="0" max="${asset.duration_seconds}" step="0.01" value="0"></label>
      <label class="field"><span>End</span><input data-field="end" type="number" min="0" max="${asset.duration_seconds}" step="0.01" value="${asset.duration_seconds}"></label>
      <label class="field"><span>Source class</span><select data-field="class">${options(state.sourceClasses,"host")}</select></label>
      <label class="field"><span>Expected action</span><select data-field="action">${options(state.routingActions,"translate")}</select></label>
      <label class="field"><span>Span notes</span><input data-field="notes" type="text"></label>
      <button data-add="1" class="primary">Apply custom</button>
    </div>
    <table class="span-table"><thead><tr><th>Time</th><th>Class</th><th>Action</th><th>Notes</th><th></th></tr></thead><tbody></tbody></table>
    <div class="coverage"></div>`;
  $("assets").appendChild(box);
  const audio = box.querySelector("audio");
  audio.ontimeupdate = () => { box.querySelector(".time-readout strong").textContent = `${audio.currentTime.toFixed(3)}s`; drawWaveform(box, asset, spans, audio.currentTime); };
  box.querySelector('[data-mark="start"]').onclick = () => setSelection(box, asset, audio.currentTime, Number(box.querySelector('[data-field="end"]').value));
  box.querySelector('[data-mark="end"]').onclick = () => setSelection(box, asset, Number(box.querySelector('[data-field="start"]').value), audio.currentTime);
  box.querySelector('[data-full]').onclick = () => setSelection(box, asset, 0, asset.duration_seconds);
  box.querySelector('[data-field="class"]').onchange = event => { box.querySelector('[data-field="action"]').value = defaultActions[event.target.value]; };
  box.querySelector('[data-add]').onclick = () => applyCustom(box, asset);
  box.querySelectorAll('[data-paint]').forEach(button => button.onclick = () => { const [sourceClass,action]=button.dataset.paint.split("|"); paintSelection(box,asset,sourceClass,action,""); });
  const canvas = box.querySelector("canvas");
  let dragStart = null;
  const timeAt = event => { const rect=canvas.getBoundingClientRect(); return Math.max(0,Math.min(asset.duration_seconds,((event.clientX-rect.left)/rect.width)*asset.duration_seconds)); };
  canvas.onmousedown = event => { dragStart=timeAt(event); setSelection(box,asset,dragStart,dragStart); };
  canvas.onmousemove = event => { if(dragStart===null)return; setSelection(box,asset,Math.min(dragStart,timeAt(event)),Math.max(dragStart,timeAt(event))); };
  canvas.onmouseup = event => { if(dragStart===null)return; const end=timeAt(event); setSelection(box,asset,Math.min(dragStart,end),Math.max(dragStart,end)); audio.currentTime=Math.min(dragStart,end); dragStart=null; };
  canvas.onmouseleave = () => { dragStart=null; };
  loadWaveform(canvas, asset, spans, audio);
  renderSpanRows(box, asset);
}
async function loadWaveform(canvas, asset, spans, audio) {
  try {
    const buffer = await (await fetch(asset.audio_url)).arrayBuffer();
    const context = new AudioContext(); const decoded = await context.decodeAudioData(buffer.slice(0));
    state.waveforms.set(asset.utterance_id, decoded.getChannelData(0)); await context.close(); drawWaveform(canvas.closest(".asset"), asset, spans, audio.currentTime);
  } catch (_) { drawWaveform(canvas.closest(".asset"), asset, spans, audio.currentTime); }
}
function drawWaveform(box, asset, spans, currentTime) {
  const canvas=box.querySelector("canvas"), ctx=canvas.getContext("2d"), w=canvas.width, h=canvas.height; ctx.clearRect(0,0,w,h); ctx.fillStyle="#f8fafc"; ctx.fillRect(0,0,w,h);
  const data=state.waveforms.get(asset.utterance_id); if(data){ctx.strokeStyle="#667085";ctx.beginPath();const step=Math.max(1,Math.floor(data.length/w));for(let x=0;x<w;x++){let peak=0;for(let j=x*step;j<Math.min(data.length,(x+1)*step);j++)peak=Math.max(peak,Math.abs(data[j]));ctx.moveTo(x,h/2-peak*h*.45);ctx.lineTo(x,h/2+peak*h*.45);}ctx.stroke();}
  (assetAnnotation(asset.utterance_id).spans||[]).forEach(span=>{const x=span.start_seconds/asset.duration_seconds*w, width=(span.end_seconds-span.start_seconds)/asset.duration_seconds*w;ctx.globalAlpha=.28;ctx.fillStyle=colors[span.source_class]||"#999";ctx.fillRect(x,0,width,h);ctx.globalAlpha=1;});
  const selection=state.selections.get(asset.utterance_id); if(selection && selection.end>selection.start){const x=selection.start/asset.duration_seconds*w,width=(selection.end-selection.start)/asset.duration_seconds*w;ctx.fillStyle="rgba(17,24,39,.13)";ctx.fillRect(x,0,width,h);ctx.strokeStyle="#111827";ctx.strokeRect(x+.5,.5,Math.max(1,width-1),h-1);}
  ctx.strokeStyle="#111827";ctx.beginPath();const cursor=(currentTime/asset.duration_seconds)*w;ctx.moveTo(cursor,0);ctx.lineTo(cursor,h);ctx.stroke();
}
function setSelection(box,asset,start,end){start=Math.max(0,Math.min(asset.duration_seconds,Number(start)||0));end=Math.max(0,Math.min(asset.duration_seconds,Number(end)||0));if(end<start)[start,end]=[end,start];box.querySelector('[data-field="start"]').value=start.toFixed(3);box.querySelector('[data-field="end"]').value=end.toFixed(3);state.selections.set(asset.utterance_id,{start,end});drawWaveform(box,asset,assetAnnotation(asset.utterance_id).spans||[],box.querySelector("audio").currentTime);}
function ensureCoverageBase(asset,spans){if(spans.length)return spans;return [{span_id:`span-base-${Date.now()}`,start_seconds:0,end_seconds:asset.duration_seconds,source_class:"uncertain",routing_action:"exclude",notes:""}];}
function mergeAdjacent(spans){const sorted=spans.filter(s=>s.end_seconds-s.start_seconds>.001).sort((a,b)=>a.start_seconds-b.start_seconds),out=[];sorted.forEach(span=>{const prev=out[out.length-1];if(prev&&Math.abs(prev.end_seconds-span.start_seconds)<=.002&&prev.source_class===span.source_class&&prev.routing_action===span.routing_action&&prev.notes===span.notes){prev.end_seconds=span.end_seconds;}else out.push({...span});});return out;}
function paintSelection(box,asset,sourceClass,routingAction,notes){const start=Number(box.querySelector('[data-field="start"]').value),end=Number(box.querySelector('[data-field="end"]').value);if(!(end>start)){showError("Select a non-empty time range.");return;}const ann=assetAnnotation(asset.utterance_id),base=ensureCoverageBase(asset,ann.spans||[]),next=[];base.forEach(span=>{if(span.end_seconds<=start||span.start_seconds>=end){next.push(span);return;}if(span.start_seconds<start)next.push({...span,end_seconds:start});if(span.end_seconds>end)next.push({...span,start_seconds:end});});next.push({span_id:`span-${Date.now()}`,start_seconds:start,end_seconds:end,source_class:sourceClass,routing_action:routingAction,notes});ann.spans=mergeAdjacent(next);ann.coverage_gaps=[];renderSpanRows(box,asset);drawWaveform(box,asset,ann.spans,box.querySelector("audio").currentTime);queueAutoSave();}
function applyCustom(box,asset){paintSelection(box,asset,box.querySelector('[data-field="class"]').value,box.querySelector('[data-field="action"]').value,box.querySelector('[data-field="notes"]').value);}
function showError(value){const message=$("message");message.className="message error";message.textContent=value;}
function queueAutoSave(){clearTimeout(state.saveTimer);state.saveTimer=setTimeout(()=>save("draft",true),500);}
function renderSpanRows(box, asset) {
  const ann=assetAnnotation(asset.utterance_id), tbody=box.querySelector("tbody"); tbody.innerHTML="";
  (ann.spans||[]).forEach((span,index)=>{const row=document.createElement("tr");row.innerHTML=`<td><button data-select="${index}">${Number(span.start_seconds).toFixed(3)}–${Number(span.end_seconds).toFixed(3)}</button></td><td>${esc(span.source_class)}</td><td>${esc(span.routing_action)}</td><td>${esc(span.notes||"")}</td><td><button data-clear="${index}">Mark uncertain</button></td>`;row.querySelector('[data-select]').onclick=()=>setSelection(box,asset,span.start_seconds,span.end_seconds);row.querySelector('[data-clear]').onclick=()=>{setSelection(box,asset,span.start_seconds,span.end_seconds);paintSelection(box,asset,"uncertain","exclude","");};tbody.appendChild(row);});
  const uncertain=(ann.spans||[]).filter(s=>s.source_class==="uncertain").reduce((sum,s)=>sum+s.end_seconds-s.start_seconds,0);const gaps=ann.coverage_gaps||[]; const coverage=box.querySelector(".coverage"); coverage.className="coverage"+((gaps.length||uncertain>.001)?" warn":""); coverage.textContent=gaps.length?`Server gaps: ${gaps.map(g=>`${g.start_seconds}–${g.end_seconds}`).join(", ")}`:uncertain>.001?`${uncertain.toFixed(2)}s remains uncertain/excluded.`:"Full labeled coverage.";
}
function payload(status) {
  const item=task(), ann=annotation(item.sample_id), assets={};
  item.audio_assets.forEach(asset=>{assets[asset.utterance_id]={spans:assetAnnotation(asset.utterance_id).spans||[]};});
  return {sample_id:item.sample_id,status,notes:$("caseNotes").value,assets};
}
async function save(status,quiet=false) {
  if(!quiet) clearTimeout(state.saveTimer);
  const message=$("message"); if(!quiet){message.className="message"; message.textContent="Saving…";}
  try { const response=await fetch("/api/annotation",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload(status))}); const data=await response.json(); if(!response.ok) throw new Error(data.error||"save failed"); state.annotations[state.currentId]=data.annotation; renderSidebar(); document.querySelectorAll(".asset").forEach(box=>{const asset=task().audio_assets.find(item=>item.utterance_id===box.dataset.utteranceId);if(asset)renderSpanRows(box,asset);}); if(!quiet)message.textContent=status==="complete"?"Case complete.":"Draft saved."; }
  catch(error){message.className="message error";message.textContent=error.message;}
}
$("saveDraft").onclick=()=>save("draft"); $("markComplete").onclick=()=>save("complete");
async function init(){const response=await fetch("/api/state");const data=await response.json();state.tasks=data.tasks;state.annotations=data.annotations||{};state.sourceClasses=data.source_classes;state.routingActions=data.routing_actions;state.currentId=state.tasks[0]?.sample_id||"";render();}
init();
</script>
</body></html>"""


def build_audio_map(
    tasks: list[dict[str, Any]],
    *,
    manifest: dict[str, Any],
    project_root: Path,
) -> dict[str, tuple[Path, int, str]]:
    cases = {str(case.get("sample_id") or ""): case for case in manifest["cases"] if isinstance(case, dict)}
    audio_map: dict[str, tuple[Path, int, str]] = {}
    for task in tasks:
        sample_id = str(task["sample_id"])
        case = cases[sample_id]
        for index, asset in enumerate(case["audio_assets"], start=1):
            audio_path = Path(str(asset.get("audio_path") or ""))
            if not audio_path.is_absolute():
                audio_path = project_root / audio_path
            verify_audio_asset(asset, audio_path)
            audio_map[f"{sample_id}-{index}"] = (
                audio_path,
                int(asset["size_bytes"]),
                str(asset["sha256"]),
            )
    return audio_map


class RoutingHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        manifest_path: Path,
        annotation_path: Path,
    ):
        manifest = load_manifest(manifest_path)
        tasks = build_routing_tasks(manifest, project_root=PROJECT_ROOT)
        self.tasks = tasks
        self.audio_map = build_audio_map(tasks, manifest=manifest, project_root=PROJECT_ROOT)
        self.store = RoutingAnnotationStore(
            path=annotation_path,
            manifest_path=manifest_path,
            tasks=tasks,
        )
        super().__init__(server_address, RoutingRequestHandler)


class RoutingRequestHandler(BaseHTTPRequestHandler):
    server: RoutingHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_text(HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            snapshot = self.server.store.snapshot()
            self._send_json(
                {
                    "tasks": self.server.tasks,
                    "source_classes": list(SOURCE_CLASSES),
                    "routing_actions": list(ROUTING_ACTIONS),
                    "annotation_path": str(self.server.store.path.resolve(strict=False)),
                    "annotations": snapshot.get("annotations", {}),
                }
            )
            return
        if parsed.path.startswith("/audio/"):
            self._serve_audio(parsed.path)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/annotation":
            self._send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        try:
            annotation = self.server.store.update(self._read_json_body())
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._send_json({"annotation": annotation})

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length < 1 or length > 2 * 1024 * 1024:
            raise ValueError("invalid request body length")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def _serve_audio(self, request_path: str) -> None:
        key = unquote(request_path.removeprefix("/audio/")).removesuffix(".wav")
        frozen = self.server.audio_map.get(key)
        if frozen is None or not frozen[0].is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "audio not found")
            return
        path, size_bytes, sha256 = frozen
        try:
            verify_audio_asset(
                {"size_bytes": size_bytes, "sha256": sha256},
                path,
            )
        except ValueError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, value: str, content_type: str) -> None:
        data = value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, value: dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        data = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve routing time-span annotation UI.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        server = RoutingHTTPServer(
            (args.host, args.port),
            manifest_path=args.manifest,
            annotation_path=args.annotations,
        )
    except (OSError, ValueError) as exc:
        print(f"Failed to start routing span server: {exc}", file=sys.stderr)
        return 1
    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"Serving {len(server.tasks)} routing cases at {url}")
    print(f"Writing routing annotations to {args.annotations}")
    if args.open:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
