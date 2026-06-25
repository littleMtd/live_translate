from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
import unicodedata
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
ANNOTATION_SCHEMA_VERSION = 1
DEFAULT_CONTEXT_TAG_OPTIONS = [
    "clip_audio",
    "bgm_mixed",
    "multi_speaker",
    "unclear_audio",
    "over_attributed_chunks",
]
DEFAULT_SPEAKER_SOURCE_OPTIONS = [
    "host_only",
    "clip_or_other_speaker",
    "host_over_clip",
    "multi_streamer",
    "speaker_unclear",
    "wrong_speaker_selected",
    "audio_source_mismatch",
]

HANGUL_BASE = 0xAC00
HANGUL_END = 0xD7A3
HANGUL_VOWELS = 21
HANGUL_FINALS = 28
HANGUL_BLOCK = HANGUL_VOWELS * HANGUL_FINALS

INITIAL_ROMAJA = [
    "g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s",
    "ss", "", "j", "jj", "ch", "k", "t", "p", "h",
]
VOWEL_ROMAJA = [
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa",
    "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i",
]
FINAL_ROMAJA = [
    "", "k", "k", "k", "n", "n", "n", "t", "l", "k",
    "m", "l", "l", "l", "p", "l", "m", "p", "p", "t",
    "t", "ng", "t", "t", "k", "t", "p", "t",
]
JAMO_ROMAJA = {
    "ㄱ": "g", "ㄲ": "kk", "ㄴ": "n", "ㄷ": "d", "ㄸ": "tt", "ㄹ": "r",
    "ㅁ": "m", "ㅂ": "b", "ㅃ": "pp", "ㅅ": "s", "ㅆ": "ss", "ㅇ": "ng",
    "ㅈ": "j", "ㅉ": "jj", "ㅊ": "ch", "ㅋ": "k", "ㅌ": "t", "ㅍ": "p",
    "ㅎ": "h", "ㅏ": "a", "ㅐ": "ae", "ㅑ": "ya", "ㅒ": "yae", "ㅓ": "eo",
    "ㅔ": "e", "ㅕ": "yeo", "ㅖ": "ye", "ㅗ": "o", "ㅘ": "wa", "ㅙ": "wae",
    "ㅚ": "oe", "ㅛ": "yo", "ㅜ": "u", "ㅝ": "wo", "ㅞ": "we", "ㅟ": "wi",
    "ㅠ": "yu", "ㅡ": "eu", "ㅢ": "ui", "ㅣ": "i",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Labeling Review</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #1f2933;
      --muted: #5c6b7a;
      --blue: #2563eb;
      --green: #168a4a;
      --red: #c2413a;
      --amber: #a15c07;
      --ink: #111827;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; height: 100%; }
    body {
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    button, textarea, input { font: inherit; }
    button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      min-height: 34px;
      border-radius: 6px;
      padding: 6px 10px;
      cursor: pointer;
    }
    button:hover { border-color: #aab5c4; }
    button.primary {
      background: var(--blue);
      border-color: var(--blue);
      color: #fff;
    }
    button:disabled {
      cursor: default;
      opacity: 0.55;
    }
    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
    }
    .topbar {
      display: grid;
      grid-template-columns: minmax(180px, 1fr) auto;
      gap: 16px;
      align-items: center;
      padding: 10px 16px;
      background: #fff;
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    .title {
      min-width: 0;
      display: flex;
      gap: 10px;
      align-items: baseline;
      white-space: nowrap;
      overflow: hidden;
    }
    .title strong { color: var(--ink); }
    .title span {
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(220px, 280px) minmax(0, 1fr);
      gap: 0;
      min-height: 0;
    }
    .sidebar {
      border-right: 1px solid var(--line);
      background: #fff;
      min-height: 0;
      overflow: auto;
    }
    .summary {
      padding: 12px;
      border-bottom: 1px solid var(--line);
      display: grid;
      gap: 8px;
    }
    .meter {
      height: 8px;
      background: #e8edf4;
      border-radius: 999px;
      overflow: hidden;
    }
    .meter div {
      height: 100%;
      background: var(--green);
      width: 0%;
    }
    .sample-list {
      display: grid;
      gap: 1px;
      background: var(--line);
    }
    .sample-row {
      border: 0;
      border-radius: 0;
      background: #fff;
      text-align: left;
      padding: 8px 10px;
      display: grid;
      grid-template-columns: 52px 1fr;
      gap: 8px;
      min-height: 42px;
    }
    .sample-row.active {
      background: #eaf1ff;
      outline: 2px solid #8bb3ff;
      outline-offset: -2px;
    }
    .sample-row .sample-id {
      font-weight: 700;
      color: var(--ink);
    }
    .sample-row .sample-label {
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    main {
      min-width: 0;
      overflow: auto;
      padding: 16px;
    }
    .work {
      max-width: 1180px;
      margin: 0 auto;
      display: grid;
      gap: 14px;
    }
    .section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .meta {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .warnings {
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }
    .instructions {
      display: grid;
      gap: 6px;
      border-left: 3px solid var(--blue);
      padding-left: 10px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .instructions strong {
      color: var(--ink);
    }
    .instructions ul {
      margin: 0;
      padding-left: 18px;
    }
    .warning {
      border: 1px solid #f2c169;
      background: #fff8e8;
      color: #713f12;
      border-radius: 6px;
      padding: 8px 10px;
      overflow-wrap: anywhere;
    }
    .field {
      min-width: 0;
      display: grid;
      gap: 2px;
    }
    .field .k {
      color: var(--muted);
      font-size: 12px;
    }
    .field .v {
      color: var(--ink);
      overflow-wrap: anywhere;
    }
    .texts {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .text-block {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      min-height: 120px;
      background: #fbfcfe;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
    .text-block h2, .audio h2, .annotation h2 {
      margin: 0 0 8px;
      font-size: 13px;
      color: var(--muted);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .chunks {
      display: grid;
      gap: 10px;
    }
    .chunk {
      display: grid;
      grid-template-columns: minmax(130px, 180px) minmax(220px, 1fr) minmax(180px, 260px);
      gap: 10px;
      align-items: center;
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }
    .chunk.prior-overlap {
      background: #fff8e8;
      border: 1px solid #f2c169;
      border-radius: 6px;
      padding: 10px;
    }
    .chunk:first-child {
      border-top: 0;
      padding-top: 0;
    }
    audio {
      width: 100%;
      min-width: 0;
      height: 34px;
    }
    .confidence {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      justify-content: flex-end;
    }
    .pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      color: var(--muted);
      background: #fff;
      white-space: nowrap;
    }
    .pill.role-primary { border-color: var(--green); color: #14532d; background: #e9f8ef; }
    .pill.role-prior-overlap { border-color: var(--amber); color: #713f12; background: #fff4d8; }
    .pill.role-supporting { border-color: #8aa0b8; color: #344054; background: #f4f7fb; }
    .labels {
      display: grid;
      grid-template-columns: repeat(5, minmax(110px, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }
    .label-button[data-label="a_translation_error"].active { background: #ffecea; border-color: var(--red); color: #7f1d1d; }
    .label-button[data-label="b_stt_error"].active { background: #fff4d8; border-color: var(--amber); color: #713f12; }
    .label-button[data-label="both"].active { background: #f1e8ff; border-color: #7c3aed; color: #4c1d95; }
    .label-button[data-label="ok"].active { background: #e9f8ef; border-color: var(--green); color: #14532d; }
    .label-button[data-label="unclear"].active { background: #eef2f7; border-color: #667085; color: #344054; }
    .context-tags {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }
    .tag-group-title {
      color: var(--muted);
      font-size: 12px;
      margin: 8px 0 6px;
    }
    .tag-button.active {
      background: #eef6ff;
      border-color: var(--blue);
      color: #1d4ed8;
    }
    textarea {
      width: 100%;
      min-height: 84px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      color: var(--ink);
      background: #fff;
    }
    .forms {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 12px;
    }
    .status {
      color: var(--muted);
      min-width: 90px;
      text-align: right;
    }
    .hidden { display: none; }
    @media (max-width: 900px) {
      .topbar, .layout, .texts, .forms { grid-template-columns: 1fr; }
      .sidebar { border-right: 0; border-bottom: 1px solid var(--line); max-height: 240px; }
      .chunk { grid-template-columns: 1fr; }
      .confidence { justify-content: flex-start; }
      .labels { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      main { padding: 10px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="title">
        <strong>Labeling Review</strong>
        <span id="samplePath"></span>
      </div>
      <div class="toolbar">
        <button id="prevBtn" type="button">Prev</button>
        <button id="nextBtn" type="button">Next</button>
        <button id="nextOpenBtn" type="button">Next Open</button>
        <button id="saveBtn" type="button" class="primary">Save</button>
        <span id="saveStatus" class="status"></span>
      </div>
    </header>
    <div class="layout">
      <aside class="sidebar">
        <div class="summary">
          <div><strong id="progressText">0 / 0</strong></div>
          <div class="meter"><div id="progressMeter"></div></div>
          <div id="samplingText"></div>
        </div>
        <div id="sampleList" class="sample-list"></div>
      </aside>
      <main>
        <div class="work">
          <section class="section">
            <div id="instructions" class="instructions"></div>
          </section>
          <section class="section">
            <div id="meta" class="meta"></div>
            <div id="warnings" class="warnings"></div>
          </section>
          <section class="section texts">
            <div class="text-block"><h2>Source</h2><div id="sourceText"></div></div>
            <div class="text-block"><h2>Romanization</h2><div id="romanizedText"></div></div>
            <div class="text-block"><h2>Translation</h2><div id="targetText"></div></div>
          </section>
          <section class="section audio">
            <h2>Audio</h2>
            <div id="chunks" class="chunks"></div>
          </section>
          <section class="section annotation">
            <h2>Annotation</h2>
            <div id="labels" class="labels"></div>
            <div class="tag-group-title">Context</div>
            <div id="contextTags" class="context-tags"></div>
            <div class="tag-group-title">Speaker / Source</div>
            <div id="speakerSourceTags" class="context-tags"></div>
            <div class="forms">
              <div class="field">
                <span class="k">Heard source text</span>
                <textarea id="heardSource"></textarea>
              </div>
              <div class="field">
                <span class="k">Notes</span>
                <textarea id="notes"></textarea>
              </div>
            </div>
          </section>
        </div>
      </main>
    </div>
  </div>
  <script src="/app.js"></script>
</body>
</html>
"""


APP_JS = r"""const state = {
  samples: [],
  annotations: {},
  labels: [],
  contextTags: [],
  speakerSourceTags: [],
  annotationFocus: "label",
  annotationGoal: "",
  annotationRules: [],
  speakerPolicy: "",
  current: 0,
  saveTimer: null,
  dirty: false
};

const labelNames = {
  a_translation_error: "A Translation",
  b_stt_error: "B STT",
  both: "Both",
  ok: "OK",
  unclear: "Unclear"
};

const keysToLabels = {
  "1": "a_translation_error",
  "2": "b_stt_error",
  "3": "both",
  "4": "ok",
  "5": "unclear"
};

const contextTagNames = {
  clip_audio: "Clip audio",
  bgm_mixed: "BGM mixed",
  multi_speaker: "Multi speaker",
  unclear_audio: "Unclear audio",
  over_attributed_chunks: "Over-attributed"
};

const speakerSourceTagNames = {
  host_only: "Host only",
  clip_or_other_speaker: "Clip/other speaker",
  host_over_clip: "Host over clip",
  multi_streamer: "Multi streamer",
  speaker_unclear: "Speaker unclear",
  wrong_speaker_selected: "Wrong speaker selected",
  audio_source_mismatch: "Audio/source mismatch"
};

function $(id) {
  return document.getElementById(id);
}

function text(value) {
  return value === null || value === undefined || value === "" ? "-" : String(value);
}

function currentSample() {
  return state.samples[state.current];
}

function annotationFor(sampleId) {
  if (!state.annotations[sampleId]) {
    state.annotations[sampleId] = {
      label: "",
      context_tags: [],
      speaker_source_tags: [],
      heard_source_text: "",
      notes: ""
    };
  }
  if (!Array.isArray(state.annotations[sampleId].context_tags)) {
    state.annotations[sampleId].context_tags = [];
  }
  if (!Array.isArray(state.annotations[sampleId].speaker_source_tags)) {
    state.annotations[sampleId].speaker_source_tags = [];
  }
  return state.annotations[sampleId];
}

function setStatus(message) {
  $("saveStatus").textContent = message;
}

function labeledCount() {
  return state.samples.filter(sample => isCurrentFocusComplete(sample.sample_id)).length;
}

function isSpeakerSourceFocus() {
  return state.annotationFocus === "speaker_source";
}

function isCurrentFocusComplete(sampleId) {
  const annotation = annotationFor(sampleId);
  if (isSpeakerSourceFocus()) {
    return annotation.speaker_source_tags.length > 0;
  }
  return Boolean(annotation.label);
}

function renderList() {
  const list = $("sampleList");
  list.innerHTML = "";
  state.samples.forEach((sample, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "sample-row" + (index === state.current ? " active" : "");
    button.addEventListener("click", () => {
      navigate(index);
    });
    const annotation = annotationFor(sample.sample_id);
    const label = isSpeakerSourceFocus()
      ? (annotation.speaker_source_tags.join(", ") || "open")
      : (annotation.label || "open");
    button.innerHTML = `<span class="sample-id">${sample.sample_id}</span><span class="sample-label">${text(label)}</span>`;
    list.appendChild(button);
  });
}

function renderProgress(payload) {
  const done = labeledCount();
  const total = state.samples.length;
  $("progressText").textContent = `${done} / ${total}`;
  $("progressMeter").style.width = total ? `${Math.round(done * 100 / total)}%` : "0%";
  if (payload) {
    $("samplingText").textContent = `seed ${text(payload.sampling.seed)} - population ${text(payload.sampling.population_size)}`;
  }
}

function renderInstructions() {
  const container = $("instructions");
  const rules = Array.isArray(state.annotationRules) ? state.annotationRules : [];
  const policy = state.speakerPolicy ? `<strong>Speaker policy:</strong> ${text(state.speakerPolicy)}` : "";
  const goal = state.annotationGoal ? `<strong>Goal:</strong> ${text(state.annotationGoal)}` : "";
  const ruleList = rules.length
    ? `<ul>${rules.map(rule => `<li>${text(rule)}</li>`).join("")}</ul>`
    : "";
  container.innerHTML = [policy, goal, ruleList].filter(Boolean).join("");
}

function renderMeta(sample) {
  const rows = [
    ["sample", sample.sample_id],
    ["original sample", sample.original_sample_id],
    ["sequence", sample.sequence_id],
    ["run", sample.run_id],
    ["profile", sample.profile_id],
    ["model", sample.model],
    ["status", sample.status],
    ["quality", sample.quality_score],
    ["current chunks", `${sample.unique_source_utterance_id_count}/${sample.source_utterance_id_count}`],
    ["evidence chunks", `${sample.unique_evidence_source_utterance_id_count || 0}/${sample.evidence_source_utterance_id_count || 0}`]
  ];
  $("meta").innerHTML = rows.map(([key, value]) => (
    `<div class="field"><span class="k">${key}</span><span class="v">${text(value)}</span></div>`
  )).join("");
}

function renderWarnings(sample) {
  const warnings = $("warnings");
  warnings.innerHTML = "";
  if (sample.source_overlap_warning) {
    const warning = document.createElement("div");
    warning.className = "warning";
    warning.textContent = `Evidence/prior overlap chunks: ${text((sample.prior_overlap_utterance_ids || []).join(", "))}`;
    warnings.appendChild(warning);
  }
}

function roleName(role) {
  if (role === "primary") {
    return "primary";
  }
  if (role === "prior_overlap") {
    return "prior overlap";
  }
  if (role === "evidence") {
    return "evidence";
  }
  return "supporting";
}

function roleClass(role) {
  if (role === "primary" || role === "prior_overlap" || role === "evidence") {
    return role.replace("_", "-");
  }
  return "supporting";
}

function renderChunks(sample) {
  const chunks = $("chunks");
  chunks.innerHTML = "";
  if (!sample.source_chunks || !sample.source_chunks.length) {
    chunks.textContent = "No audio chunks.";
    return;
  }
  sample.source_chunks.forEach((chunk, index) => {
    const row = document.createElement("div");
    row.className = "chunk" + (chunk.chunk_role === "prior_overlap" ? " prior-overlap" : "");
    const avg = chunk.avg_logprob === null || chunk.avg_logprob === undefined ? "-" : Number(chunk.avg_logprob).toFixed(3);
    const noSpeech = chunk.no_speech_prob === null || chunk.no_speech_prob === undefined ? "-" : Number(chunk.no_speech_prob).toFixed(3);
    const seconds = chunk.stt_audio_seconds === null || chunk.stt_audio_seconds === undefined ? "-" : `${Number(chunk.stt_audio_seconds).toFixed(2)}s`;
    const priorSeqs = Array.isArray(chunk.prior_translation_sequence_ids) ? chunk.prior_translation_sequence_ids.join(", ") : "";
    const prior = priorSeqs ? `<span class="pill">prior seq ${priorSeqs}</span>` : "";
    const role = chunk.chunk_role || "supporting";
    const sourceKind = chunk.source_kind || "current";
    const audio = chunk.audio_exists
      ? `<audio controls preload="metadata" src="${chunk.audio_url}"></audio>`
      : `<span class="pill">missing wav</span>`;
    row.innerHTML = `
      <div class="field"><span class="k">chunk ${index + 1}</span><span class="v">${text(chunk.utterance_id)}</span></div>
      <div>${audio}</div>
      <div class="confidence">
        <span class="pill role-${roleClass(role)}">${roleName(role)}</span>
        <span class="pill">${text(sourceKind)}</span>
        ${prior}
        <span class="pill">avg ${avg}</span>
        <span class="pill">nospeech ${noSpeech}</span>
        <span class="pill">${seconds}</span>
      </div>
    `;
    chunks.appendChild(row);
  });
}

function renderContextTags(sample) {
  const container = $("contextTags");
  const annotation = annotationFor(sample.sample_id);
  container.innerHTML = "";
  state.contextTags.forEach(tag => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tag-button" + (annotation.context_tags.includes(tag) ? " active" : "");
    button.textContent = contextTagNames[tag] || tag;
    button.addEventListener("click", () => {
      collectForm();
      const tagSet = new Set(annotation.context_tags);
      if (tagSet.has(tag)) {
        tagSet.delete(tag);
      } else {
        tagSet.add(tag);
      }
      annotation.context_tags = state.contextTags.filter(value => tagSet.has(value));
      queueSave();
      render();
    });
    container.appendChild(button);
  });
}

function renderSpeakerSourceTags(sample) {
  const container = $("speakerSourceTags");
  const annotation = annotationFor(sample.sample_id);
  container.innerHTML = "";
  state.speakerSourceTags.forEach(tag => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tag-button" + (annotation.speaker_source_tags.includes(tag) ? " active" : "");
    button.textContent = speakerSourceTagNames[tag] || tag;
    button.addEventListener("click", () => {
      collectForm();
      const tagSet = new Set(annotation.speaker_source_tags);
      if (tagSet.has(tag)) {
        tagSet.delete(tag);
      } else {
        tagSet.add(tag);
      }
      annotation.speaker_source_tags = state.speakerSourceTags.filter(value => tagSet.has(value));
      queueSave();
      render();
    });
    container.appendChild(button);
  });
}

function renderLabels(sample) {
  const labels = $("labels");
  const annotation = annotationFor(sample.sample_id);
  labels.innerHTML = "";
  state.labels.forEach((label, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "label-button" + (annotation.label === label ? " active" : "");
    button.dataset.label = label;
    button.textContent = `${index + 1} ${labelNames[label] || label}`;
    button.addEventListener("click", () => {
      collectForm();
      annotation.label = label;
      queueSave();
      render();
      goNextOpenSoon();
    });
    labels.appendChild(button);
  });
}

function render(payload) {
  const sample = currentSample();
  if (!sample) {
    return;
  }
  localStorage.setItem("labelingReview.currentSampleId", sample.sample_id);
  $("samplePath").textContent = state.samplePath || "";
  renderProgress(payload);
  renderList();
  renderInstructions();
  renderMeta(sample);
  renderWarnings(sample);
  $("sourceText").textContent = text(sample.source_text);
  $("romanizedText").textContent = text(sample.romanized_source_text);
  $("targetText").textContent = text(sample.target_text);
  renderChunks(sample);
  renderLabels(sample);
  renderContextTags(sample);
  renderSpeakerSourceTags(sample);
  const annotation = annotationFor(sample.sample_id);
  $("heardSource").value = annotation.heard_source_text || "";
  $("notes").value = annotation.notes || "";
  $("prevBtn").disabled = state.current === 0;
  $("nextBtn").disabled = state.current >= state.samples.length - 1;
}

function collectForm() {
  const sample = currentSample();
  if (!sample) {
    return null;
  }
  const annotation = annotationFor(sample.sample_id);
  annotation.heard_source_text = $("heardSource").value;
  annotation.notes = $("notes").value;
  annotation.context_tags = Array.isArray(annotation.context_tags) ? annotation.context_tags : [];
  annotation.speaker_source_tags = Array.isArray(annotation.speaker_source_tags) ? annotation.speaker_source_tags : [];
  return { sample_id: sample.sample_id, ...annotation };
}

async function saveNow() {
  const payload = collectForm();
  if (!payload) {
    return;
  }
  setStatus("Saving");
  const response = await fetch("/api/annotation", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    setStatus("Save failed");
    return;
  }
  const data = await response.json();
  state.annotations = data.annotations || state.annotations;
  state.dirty = false;
  setStatus("Saved");
  renderProgress();
  renderList();
}

function queueSave() {
  state.dirty = true;
  setStatus("Unsaved");
  window.clearTimeout(state.saveTimer);
  state.saveTimer = window.setTimeout(() => {
    saveNow().catch(() => setStatus("Save failed"));
  }, 250);
}

function navigate(index) {
  if (index < 0 || index >= state.samples.length) {
    return;
  }
  if (state.dirty) {
    saveNow().catch(() => setStatus("Save failed"));
  }
  state.current = index;
  render();
}

function nextOpenIndex() {
  for (let offset = 1; offset <= state.samples.length; offset += 1) {
    const index = (state.current + offset) % state.samples.length;
    const sample = state.samples[index];
    if (!isCurrentFocusComplete(sample.sample_id)) {
      return index;
    }
  }
  return state.current;
}

function goNextOpenSoon() {
  window.setTimeout(() => {
    const index = nextOpenIndex();
    if (index !== state.current) {
      navigate(index);
    }
  }, 160);
}

function attachEvents() {
  $("prevBtn").addEventListener("click", () => navigate(state.current - 1));
  $("nextBtn").addEventListener("click", () => navigate(state.current + 1));
  $("nextOpenBtn").addEventListener("click", () => navigate(nextOpenIndex()));
  $("saveBtn").addEventListener("click", () => saveNow().catch(() => setStatus("Save failed")));
  $("heardSource").addEventListener("input", queueSave);
  $("notes").addEventListener("input", queueSave);
  window.addEventListener("keydown", event => {
    const tag = event.target && event.target.tagName ? event.target.tagName.toLowerCase() : "";
    if (tag === "textarea" || tag === "input") {
      return;
    }
    const sample = currentSample();
    if (!sample) {
      return;
    }
    const annotation = annotationFor(sample.sample_id);
    if (keysToLabels[event.key]) {
      event.preventDefault();
      collectForm();
      annotation.label = keysToLabels[event.key];
      queueSave();
      render();
      goNextOpenSoon();
      return;
    }
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      navigate(state.current - 1);
      return;
    }
    if (event.key === "ArrowRight") {
      event.preventDefault();
      navigate(state.current + 1);
      return;
    }
    if (event.key === " ") {
      const firstAudio = document.querySelector("audio");
      if (firstAudio) {
        event.preventDefault();
        if (firstAudio.paused) {
          firstAudio.play();
        } else {
          firstAudio.pause();
        }
      }
    }
  });
}

async function boot() {
  const response = await fetch("/api/state");
  if (!response.ok) {
    throw new Error("Failed to load state");
  }
  const payload = await response.json();
  state.samples = payload.samples || [];
  state.annotations = payload.annotations || {};
  state.labels = payload.label_options || [];
  state.contextTags = payload.context_tag_options || [];
  state.speakerSourceTags = payload.speaker_source_options || [];
  state.annotationFocus = payload.annotation_focus || "label";
  state.annotationGoal = payload.annotation_goal || "";
  state.annotationRules = payload.annotation_rules || [];
  state.speakerPolicy = payload.speaker_policy || "";
  state.samplePath = payload.sample_path || "";
  const remembered = localStorage.getItem("labelingReview.currentSampleId");
  const rememberedIndex = state.samples.findIndex(sample => sample.sample_id === remembered);
  state.current = rememberedIndex >= 0 ? rememberedIndex : 0;
  attachEvents();
  setStatus("");
  render(payload);
}

boot().catch(error => {
  document.body.innerHTML = `<pre>${error.message}</pre>`;
});
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _romanize_hangul_syllable(char: str) -> str | None:
    code = ord(char)
    if code < HANGUL_BASE or code > HANGUL_END:
        return None
    offset = code - HANGUL_BASE
    initial_index = offset // HANGUL_BLOCK
    vowel_index = (offset % HANGUL_BLOCK) // HANGUL_FINALS
    final_index = offset % HANGUL_FINALS
    return (
        INITIAL_ROMAJA[initial_index]
        + VOWEL_ROMAJA[vowel_index]
        + FINAL_ROMAJA[final_index]
    )


def romanize_korean_text(value: str) -> str:
    """Return a syllable-separated Korean romanization for review support."""
    pieces: list[str] = []
    previous_was_hangul = False
    for char in unicodedata.normalize("NFC", value):
        romanized = _romanize_hangul_syllable(char)
        if romanized is not None:
            if previous_was_hangul:
                pieces.append("-")
            pieces.append(romanized)
            previous_was_hangul = True
            continue
        jamo = JAMO_ROMAJA.get(char)
        if jamo is not None:
            if previous_was_hangul:
                pieces.append("-")
            pieces.append(jamo)
            previous_was_hangul = False
            continue
        pieces.append(char)
        previous_was_hangul = False
    return "".join(pieces)


def latest_sample_file(log_dir: Path = DEFAULT_LOG_DIR) -> Path | None:
    files = sorted(log_dir.glob("labeling_sample_*.json"), key=lambda path: path.stat().st_mtime)
    return files[-1] if files else None


def default_annotation_path(sample_path: Path) -> Path:
    return sample_path.with_suffix(".annotations.json")


def load_sample(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("sample file must contain a JSON object")
    samples = data.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("sample file must contain a non-empty samples list")
    sample_ids: set[str] = set()
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("each sample must be an object")
        sample_id = str(sample.get("sample_id") or "")
        if not sample_id:
            raise ValueError("each sample must include sample_id")
        if sample_id in sample_ids:
            raise ValueError(f"duplicate sample_id: {sample_id}")
        sample_ids.add(sample_id)
    return data


def sample_ids(sample_data: dict[str, Any]) -> set[str]:
    return {str(sample["sample_id"]) for sample in sample_data["samples"]}


def label_options(sample_data: dict[str, Any]) -> list[str]:
    values = sample_data.get("label_options")
    if isinstance(values, list):
        labels = [str(value) for value in values if str(value)]
        if labels:
            return labels
    return ["a_translation_error", "b_stt_error", "both", "ok", "unclear"]


def context_tag_options(sample_data: dict[str, Any]) -> list[str]:
    values = sample_data.get("context_tag_options")
    if isinstance(values, list):
        tags = [str(value) for value in values if str(value)]
        if tags:
            return tags
    return DEFAULT_CONTEXT_TAG_OPTIONS


def speaker_source_options(sample_data: dict[str, Any]) -> list[str]:
    values = sample_data.get("speaker_source_options")
    if isinstance(values, list):
        tags = [str(value) for value in values if str(value)]
        if tags:
            return tags
    return DEFAULT_SPEAKER_SOURCE_OPTIONS


def build_audio_map(sample_data: dict[str, Any]) -> dict[str, Path]:
    audio_map: dict[str, Path] = {}
    for sample in sample_data["samples"]:
        sample_id = str(sample["sample_id"])
        chunks = sample.get("source_chunks")
        if not isinstance(chunks, list):
            continue
        for index, chunk in enumerate(chunks, start=1):
            if not isinstance(chunk, dict):
                continue
            audio_path = str(chunk.get("audio_path") or "")
            if not audio_path:
                continue
            audio_id = f"{sample_id}-{index}"
            audio_map[audio_id] = Path(audio_path)
    return audio_map


def public_sample_data(sample_data: dict[str, Any], audio_map: dict[str, Path]) -> list[dict[str, Any]]:
    samples = copy.deepcopy(sample_data["samples"])
    for sample in samples:
        sample_id = str(sample["sample_id"])
        sample["romanized_source_text"] = romanize_korean_text(str(sample.get("source_text") or ""))
        chunks = sample.get("source_chunks")
        if not isinstance(chunks, list):
            continue
        for index, chunk in enumerate(chunks, start=1):
            if isinstance(chunk, dict) and f"{sample_id}-{index}" in audio_map:
                chunk["audio_url"] = f"/audio/{sample_id}-{index}.wav"
        indexed_chunks = list(enumerate(chunks))
        indexed_chunks.sort(
            key=lambda item: (
                item[1].get("stt_event_line")
                if isinstance(item[1], dict) and isinstance(item[1].get("stt_event_line"), int)
                else float("inf"),
                item[0],
            )
        )
        sample["source_chunks"] = [chunk for _, chunk in indexed_chunks]
    return samples


class AnnotationStore:
    def __init__(self, *, path: Path, sample_path: Path, sample_data: dict[str, Any]):
        self.path = path
        self.sample_path = sample_path
        self.sample_data = sample_data
        self.sample_ids = sample_ids(sample_data)
        self.label_options = set(label_options(sample_data))
        self.context_tag_options = set(context_tag_options(sample_data))
        self.speaker_source_options = set(speaker_source_options(sample_data))
        self.lock = threading.Lock()
        self._data = self._load()

    def _empty_data(self) -> dict[str, Any]:
        return {
            "annotation_schema": ANNOTATION_SCHEMA_VERSION,
            "sample_path": str(self.sample_path.resolve(strict=False)),
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "label_options": sorted(self.label_options),
            "context_tag_options": sorted(self.context_tag_options),
            "speaker_source_options": sorted(self.speaker_source_options),
            "annotations": {},
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_data()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid annotation file: {self.path}") from exc
        if not isinstance(data, dict):
            raise ValueError("annotation file must contain a JSON object")
        annotations = data.get("annotations")
        if not isinstance(annotations, dict):
            data["annotations"] = {}
        else:
            for value in annotations.values():
                if isinstance(value, dict):
                    value.setdefault("context_tags", [])
                    value.setdefault("speaker_source_tags", [])
        data.setdefault("annotation_schema", ANNOTATION_SCHEMA_VERSION)
        data.setdefault("sample_path", str(self.sample_path.resolve(strict=False)))
        data.setdefault("created_at", utc_now())
        data.setdefault("label_options", sorted(self.label_options))
        data.setdefault("context_tag_options", sorted(self.context_tag_options))
        data.setdefault("speaker_source_options", sorted(self.speaker_source_options))
        data["updated_at"] = utc_now()
        return data

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self._data)

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        sample_id = str(payload.get("sample_id") or "")
        if sample_id not in self.sample_ids:
            raise ValueError(f"unknown sample_id: {sample_id}")
        label = str(payload.get("label") or "")
        if label and label not in self.label_options:
            raise ValueError(f"unknown label: {label}")
        raw_context_tags = payload.get("context_tags")
        context_tags: list[str] = []
        if raw_context_tags is None:
            context_tags = []
        elif not isinstance(raw_context_tags, list):
            raise ValueError("context_tags must be a list")
        else:
            for value in raw_context_tags:
                tag = str(value)
                if tag not in self.context_tag_options:
                    raise ValueError(f"unknown context tag: {tag}")
                if tag not in context_tags:
                    context_tags.append(tag)
        raw_speaker_source_tags = payload.get("speaker_source_tags")
        speaker_source_tags: list[str] = []
        if raw_speaker_source_tags is None:
            speaker_source_tags = []
        elif not isinstance(raw_speaker_source_tags, list):
            raise ValueError("speaker_source_tags must be a list")
        else:
            for value in raw_speaker_source_tags:
                tag = str(value)
                if tag not in self.speaker_source_options:
                    raise ValueError(f"unknown speaker/source tag: {tag}")
                if tag not in speaker_source_tags:
                    speaker_source_tags.append(tag)
        annotation = {
            "label": label,
            "context_tags": context_tags,
            "speaker_source_tags": speaker_source_tags,
            "heard_source_text": str(payload.get("heard_source_text") or ""),
            "notes": str(payload.get("notes") or ""),
            "updated_at": utc_now(),
        }
        with self.lock:
            self._data["annotations"][sample_id] = annotation
            self._data["updated_at"] = utc_now()
            self._write_locked()
            return copy.deepcopy(self._data)

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)


class LabelingHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], *, sample_path: Path, annotation_path: Path):
        self.sample_path = sample_path
        self.sample_data = load_sample(sample_path)
        self.audio_map = build_audio_map(self.sample_data)
        self.store = AnnotationStore(
            path=annotation_path,
            sample_path=sample_path,
            sample_data=self.sample_data,
        )
        super().__init__(server_address, LabelingRequestHandler)


class LabelingRequestHandler(BaseHTTPRequestHandler):
    server: LabelingHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            self._send_text(INDEX_HTML, "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._send_text(APP_JS, "application/javascript; charset=utf-8")
            return
        if path == "/api/state":
            self._send_json(self._state_payload())
            return
        if path.startswith("/audio/"):
            self._serve_audio(path)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/annotation":
            self._send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        try:
            payload = self._read_json_body()
            data = self.server.store.update(payload)
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._send_json({"annotations": data["annotations"], "updated_at": data["updated_at"]})

    def _state_payload(self) -> dict[str, Any]:
        store_data = self.server.store.snapshot()
        return {
            "sample_path": str(self.server.sample_path.resolve(strict=False)),
            "annotation_path": str(self.server.store.path.resolve(strict=False)),
            "annotation_focus": self.server.sample_data.get("annotation_focus", "label"),
            "annotation_goal": self.server.sample_data.get("annotation_goal", ""),
            "annotation_rules": self.server.sample_data.get("annotation_rules", []),
            "speaker_policy": self.server.sample_data.get("speaker_policy", ""),
            "sampling": self.server.sample_data.get("sampling", {}),
            "label_options": label_options(self.server.sample_data),
            "context_tag_options": context_tag_options(self.server.sample_data),
            "speaker_source_options": speaker_source_options(self.server.sample_data),
            "samples": public_sample_data(self.server.sample_data, self.server.audio_map),
            "annotations": store_data.get("annotations", {}),
        }

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length < 1 or length > 1024 * 1024:
            raise ValueError("invalid request body length")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _serve_audio(self, request_path: str) -> None:
        name = unquote(request_path.removeprefix("/audio/"))
        if name.endswith(".wav"):
            name = name[:-4]
        audio_path = self.server.audio_map.get(name)
        if audio_path is None or not audio_path.exists():
            self._send_error(HTTPStatus.NOT_FOUND, "audio not found")
            return
        try:
            data = audio_path.read_bytes()
        except OSError:
            self._send_error(HTTPStatus.NOT_FOUND, "audio not readable")
            return
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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a local browser UI for labeling sampled translation events.")
    parser.add_argument("sample", nargs="?", type=Path, default=None, help="Path to labeling_sample_*.json.")
    parser.add_argument("--annotations", type=Path, default=None, help="Output annotations JSON path.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port.")
    parser.add_argument("--open", action="store_true", help="Open the browser after the server starts.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    sample_path = args.sample or latest_sample_file()
    if sample_path is None:
        print("No labeling_sample_*.json file found. Run scripts/sample_labeling_cases.py first.", file=sys.stderr)
        return 1
    annotation_path = args.annotations or default_annotation_path(sample_path)
    try:
        server = LabelingHTTPServer(
            (args.host, args.port),
            sample_path=sample_path,
            annotation_path=annotation_path,
        )
    except (OSError, ValueError) as exc:
        print(f"Failed to start labeling server: {exc}", file=sys.stderr)
        return 2

    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"Serving {sample_path} at {url}")
    print(f"Writing annotations to {annotation_path}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
