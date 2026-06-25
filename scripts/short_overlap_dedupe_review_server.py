from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import webbrowser
import wave
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.labeling_review_server import romanize_korean_text


DEFAULT_SHADOW = PROJECT_ROOT / ".analysis-tmp" / "short_overlap_surgical_20260624.json"
DEFAULT_ANNOTATIONS = PROJECT_ROOT / ".analysis-tmp" / "short_overlap_surgical_20260624.annotations.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DECISIONS = ("safe_dedupe", "intentional_repetition", "unclear")


HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Short Overlap Dedupe Review</title>
<style>
:root { --ink:#172033; --muted:#667085; --line:#d7ddea; --soft:#f6f8fb; --blue:#2359a8; --green:#137a48; --red:#b42318; --amber:#a15c00; }
* { box-sizing:border-box; }
body { margin:0; color:var(--ink); background:#fff; font:14px/1.5 system-ui,"Segoe UI",sans-serif; }
button,textarea { font:inherit; }
button { border:1px solid var(--line); background:#fff; border-radius:6px; padding:8px 11px; cursor:pointer; }
button:hover { border-color:#98a2b3; }
.app { display:grid; grid-template-columns:240px minmax(0,1fr); min-height:100vh; }
aside { border-right:1px solid var(--line); background:var(--soft); padding:15px 12px; position:sticky; top:0; height:100vh; overflow:auto; }
h1 { font-size:17px; margin:0 0 4px; }
.summary { color:var(--muted); margin:0 0 12px; }
.meter { height:7px; background:#e4e9f1; border-radius:5px; overflow:hidden; margin:8px 0 12px; }
.meter div { height:100%; background:var(--green); }
.list { display:grid; gap:5px; }
.case { width:100%; display:grid; grid-template-columns:45px 1fr; gap:7px; text-align:left; }
.case.active { color:var(--blue); border-color:var(--blue); font-weight:650; }
.case .state { color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
main { width:100%; max-width:1400px; padding:22px clamp(16px,3vw,42px) 60px; }
.head { display:flex; justify-content:space-between; gap:16px; margin-bottom:16px; }
.meta { color:var(--muted); }
.texts { display:grid; grid-template-columns:1fr 1fr; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
.panel { padding:14px; min-height:105px; white-space:pre-wrap; }
.panel + .panel { border-left:1px solid var(--line); }
.label { color:var(--muted); font-size:12px; text-transform:uppercase; margin-bottom:5px; }
.romanization { color:#475467; margin-top:10px; padding-top:9px; border-top:1px solid #e7eaf0; font-size:13px; overflow-wrap:anywhere; }
.removed { color:var(--red); font-weight:700; }
.shadow { margin-top:12px; border:1px solid #b8d8c6; background:#f1faf5; border-radius:8px; padding:14px; min-height:74px; white-space:pre-wrap; }
.audio-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:16px 0; }
.audio-card { border:1px solid var(--line); border-radius:8px; padding:13px; }
.audio-title { display:flex; justify-content:space-between; gap:8px; margin-bottom:8px; font-weight:650; }
audio { width:100%; }
.decision { display:grid; grid-template-columns:repeat(3,1fr); gap:9px; margin:18px 0 12px; }
.decision button { min-height:48px; font-size:15px; }
.decision button.selected.safe_dedupe { background:var(--green); border-color:var(--green); color:#fff; }
.decision button.selected.intentional_repetition { background:var(--red); border-color:var(--red); color:#fff; }
.decision button.selected.unclear { background:var(--amber); border-color:var(--amber); color:#fff; }
textarea { width:100%; min-height:80px; resize:vertical; border:1px solid var(--line); border-radius:6px; padding:9px; }
.message { min-height:22px; margin-top:8px; color:var(--green); }
.hint { color:var(--muted); margin-top:6px; }
@media(max-width:800px) { .app { grid-template-columns:1fr; } aside { position:static; height:auto; border-right:0; border-bottom:1px solid var(--line); } .list { grid-template-columns:repeat(4,1fr); } .texts,.audio-grid { grid-template-columns:1fr; } .panel + .panel { border-left:0; border-top:1px solid var(--line); } }
</style>
</head>
<body>
<div class="app">
  <aside><h1>Overlap dedupe</h1><div id="summary" class="summary"></div><div class="meter"><div id="meter"></div></div><div id="list" class="list"></div></aside>
  <main>
    <div class="head"><div><h1 id="title"></h1><div id="meta" class="meta"></div></div><button id="next">Next unresolved</button></div>
    <div class="texts"><section class="panel"><div class="label">Previous subtitle</div><div id="previous"></div><div id="previousRomanization" class="romanization"></div></section><section class="panel"><div class="label">Current subtitle</div><div id="current"></div><div id="currentRomanization" class="romanization"></div></section></div>
    <section class="shadow"><div class="label">After proposed dedupe</div><div id="shadow"></div><div id="shadowRomanization" class="romanization"></div></section>
    <div class="audio-grid"><section class="audio-card"><div class="audio-title"><span>Previous boundary WAV</span><span id="previousId" class="meta"></span></div><audio id="previousAudio" controls preload="metadata"></audio></section><section class="audio-card"><div class="audio-title"><span>Current boundary WAV</span><span id="currentId" class="meta"></span></div><audio id="currentAudio" controls preload="metadata"></audio></section></div>
    <div class="decision"><button data-value="safe_dedupe">1 Safe dedupe</button><button data-value="intentional_repetition">2 Intentional repetition</button><button data-value="unclear">3 Unclear</button></div>
    <textarea id="notes" placeholder="Optional notes"></textarea><div class="hint">判斷被標紅的開頭是否只是上一個 WAV overlap 的重複辨識。按 1/2/3 可直接儲存並前往下一筆。</div><div id="message" class="message"></div>
  </main>
</div>
<script>
const state={tasks:[],annotations:{},current:""}; const $=id=>document.getElementById(id); const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function ann(id){return state.annotations[id]||{decision:"",notes:""};}
function renderList(){const done=state.tasks.filter(t=>ann(t.candidate_id).decision).length; $("summary").textContent=`${done}/${state.tasks.length} reviewed`; $("meter").style.width=`${state.tasks.length?done/state.tasks.length*100:0}%`; $("list").innerHTML=""; state.tasks.forEach(t=>{const b=document.createElement("button"); const a=ann(t.candidate_id); b.className="case"+(t.candidate_id===state.current?" active":""); b.innerHTML=`<span>${esc(t.candidate_id)}</span><span class="state">${esc(a.decision||t.removed_prefix)}</span>`; b.onclick=()=>{state.current=t.candidate_id;render();}; $("list").appendChild(b);});}
function render(){const t=state.tasks.find(x=>x.candidate_id===state.current); if(!t)return; const a=ann(t.candidate_id); renderList(); $("title").textContent=`${t.candidate_id} · ${t.profile_id}`; $("meta").textContent=`seq ${t.previous_sequence_id} → ${t.sequence_id} · overlap ${t.overlap_seconds.toFixed(1)}s`; $("previous").textContent=t.previous_source_text; $("previousRomanization").textContent=t.previous_romanization; const prefix=t.removed_prefix; $("current").innerHTML=`<span class="removed">${esc(prefix)}</span>${esc(t.current_source_text.slice(prefix.length))}`; $("currentRomanization").textContent=t.current_romanization; $("shadow").textContent=t.shadow_source_text||"(empty subtitle)"; $("shadowRomanization").textContent=t.shadow_romanization||"(empty)"; $("previousId").textContent=t.previous_audio.utterance_id; $("currentId").textContent=t.current_audio.utterance_id; $("previousAudio").src=t.previous_audio.audio_url; $("currentAudio").src=t.current_audio.audio_url; $("notes").value=a.notes||""; document.querySelectorAll("[data-value]").forEach(b=>{b.className=b.dataset.value+(a.decision===b.dataset.value?" selected":"");}); $("message").textContent="";}
async function save(decision,advance=true){const notes=$("notes").value; const res=await fetch("/api/annotation",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({candidate_id:state.current,decision,notes})}); const data=await res.json(); if(!res.ok){$("message").textContent=data.error||"save failed";return;} state.annotations=data.annotations; $("message").textContent="Saved"; if(advance) nextUnresolved(); else render();}
function nextUnresolved(){const index=state.tasks.findIndex(t=>t.candidate_id===state.current); for(let step=1;step<=state.tasks.length;step++){const t=state.tasks[(index+step)%state.tasks.length];if(!ann(t.candidate_id).decision){state.current=t.candidate_id;render();return;}} render();}
document.querySelectorAll("[data-value]").forEach(b=>b.onclick=()=>save(b.dataset.value)); $("next").onclick=nextUnresolved; $("notes").onchange=()=>{const a=ann(state.current);if(a.decision)save(a.decision,false);}; document.addEventListener("keydown",e=>{if(e.target.tagName==="TEXTAREA")return; const map={"1":"safe_dedupe","2":"intentional_repetition","3":"unclear"};if(map[e.key])save(map[e.key]);});
fetch("/api/state").then(r=>r.json()).then(data=>{state.tasks=data.tasks;state.annotations=data.annotations;state.current=(state.tasks.find(t=>!ann(t.candidate_id).decision)||state.tasks[0]||{}).candidate_id||"";render();});
</script>
</body></html>"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audio_metadata(path: Path, utterance_id: str, audio_url: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"missing audio asset: {path}")
    with wave.open(str(path), "rb") as wav:
        duration = wav.getnframes() / float(wav.getframerate())
    return {
        "utterance_id": utterance_id,
        "audio_url": audio_url,
        "duration_seconds": round(duration, 3),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def load_shadow(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read shadow artifact {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        raise ValueError("shadow artifact must contain candidates")
    return data


def build_review_tasks(shadow: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, tuple[Path, int, str]]]:
    tasks: list[dict[str, Any]] = []
    audio_map: dict[str, tuple[Path, int, str]] = {}
    audio_root = (PROJECT_ROOT / "logs" / "audio_dump").resolve()
    review_candidates = [
        candidate
        for candidate in shadow["candidates"]
        if not candidate.get("definitions")
        or bool(candidate.get("definitions", {}).get("blunt_min4"))
    ]
    for index, candidate in enumerate(review_candidates, start=1):
        candidate_id = f"D{index:03d}"
        run_id = str(candidate["run_id"])
        previous_id = str(candidate["previous_last_source_utterance_id"])
        current_id = str(candidate["first_source_utterance_id"])
        previous_path = (audio_root / run_id / f"{previous_id}.wav").resolve()
        current_path = (audio_root / run_id / f"{current_id}.wav").resolve()
        if audio_root not in previous_path.parents or audio_root not in current_path.parents:
            raise ValueError("audio path escaped audio_dump")
        # Historical logs can outlive their optional audio dump. Keep those rows in
        # the analysis artifact, but do not make the review server unusable for the
        # candidates whose complete boundary audio still exists.
        if not previous_path.is_file() or not current_path.is_file():
            continue
        previous_key = f"{candidate_id}-previous"
        current_key = f"{candidate_id}-current"
        previous_audio = _audio_metadata(previous_path, previous_id, f"/audio/{previous_key}.wav")
        current_audio = _audio_metadata(current_path, current_id, f"/audio/{current_key}.wav")
        audio_map[previous_key] = (previous_path, previous_audio["size_bytes"], previous_audio["sha256"])
        audio_map[current_key] = (current_path, current_audio["size_bytes"], current_audio["sha256"])
        task = dict(candidate)
        if candidate.get("definitions"):
            # The surgical artifact is a sensitivity union.  The approved review
            # surface is only the proposed min4 definition, not the rejected min3 arm.
            task["removed_prefix"] = str(candidate.get("removed_prefix_min4") or "")
            task["shadow_source_text"] = str(
                candidate.get("shadow_source_text_min4")
                or candidate.get("current_source_text")
                or ""
            )
        task.update(
            {
                "candidate_id": candidate_id,
                "previous_audio": previous_audio,
                "current_audio": current_audio,
                "previous_romanization": romanize_korean_text(str(candidate.get("previous_source_text") or "")),
                "current_romanization": romanize_korean_text(str(candidate.get("current_source_text") or "")),
                "shadow_romanization": romanize_korean_text(str(candidate.get("shadow_source_text") or "")),
            }
        )
        tasks.append(task)
    return tasks, audio_map


class AnnotationStore:
    def __init__(self, path: Path, shadow_path: Path, tasks: list[dict[str, Any]]):
        self.path = path
        self.shadow_path = shadow_path
        self.shadow_sha256 = _sha256(shadow_path)
        self.task_ids = {task["candidate_id"] for task in tasks}
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("shadow_sha256") != self.shadow_sha256:
                raise ValueError("annotation file belongs to a different shadow artifact")
            return data
        return {
            "phase0_short_overlap_dedupe_annotations_schema": 2,
            "shadow_path": str(self.shadow_path.resolve()),
            "shadow_sha256": self.shadow_sha256,
            "decisions": list(DECISIONS),
            "annotations": {},
        }

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(payload.get("candidate_id") or "")
        decision = str(payload.get("decision") or "")
        if candidate_id not in self.task_ids:
            raise ValueError("unknown candidate_id")
        if decision not in DECISIONS:
            raise ValueError("unknown decision")
        annotation = {
            "decision": decision,
            "notes": str(payload.get("notes") or "").strip(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.lock:
            self.data["annotations"][candidate_id] = annotation
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
            return json.loads(json.dumps(self.data))


class ReviewHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, tasks, audio_map, store):
        super().__init__(address, ReviewHandler)
        self.tasks = tasks
        self.audio_map = audio_map
        self.store = store


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewHTTPServer

    def _json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            body = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            self._json(HTTPStatus.OK, {"tasks": self.server.tasks, "annotations": self.server.store.data["annotations"], "decisions": list(DECISIONS)})
            return
        if path.startswith("/audio/") and path.endswith(".wav"):
            key = path[len("/audio/") : -len(".wav")]
            asset = self.server.audio_map.get(key)
            if not asset:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            audio_path, size_bytes, sha256 = asset
            if audio_path.stat().st_size != size_bytes or _sha256(audio_path) != sha256:
                self.send_error(HTTPStatus.CONFLICT, "audio fingerprint changed")
                return
            body = audio_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/annotation":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            data = self.server.store.update(payload)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(HTTPStatus.OK, data)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Review short overlap transcript-dedupe candidates.")
    parser.add_argument("--shadow", type=Path, default=DEFAULT_SHADOW)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    shadow_path = args.shadow.resolve()
    tasks, audio_map = build_review_tasks(load_shadow(shadow_path))
    shadow = load_shadow(shadow_path)
    proposed_count = sum(
        1
        for candidate in shadow["candidates"]
        if not candidate.get("definitions")
        or bool(candidate.get("definitions", {}).get("blunt_min4"))
    )
    unavailable_count = proposed_count - len(tasks)
    store = AnnotationStore(args.annotations.resolve(), shadow_path, tasks)
    server = ReviewHTTPServer((args.host, args.port), tasks, audio_map, store)
    url = f"http://{args.host}:{args.port}/"
    print(
        f"Reviewing {len(tasks)}/{proposed_count} audio-ready candidates at {url} "
        f"({unavailable_count} unavailable: missing previous/current WAV)"
    )
    print(f"Annotations: {args.annotations.resolve()}")
    if not args.no_browser:
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
