# Gemini Live Translate Probe - 2026-06-18

## Scope

This is a Phase 0 offline probe only. It reads the host-primary candidate pool and
audio dumps, calls Google Gemini Live Translate, and writes comparison JSONL under
`.analysis-tmp/`.

It does not modify or route the live translation path.

## Tooling

Script:

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\gemini_live_translate_probe.py --dry-run --limit 3 --output .analysis-tmp\gemini_live_translate_probe_dryrun.jsonl
```

Live API probe:

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\gemini_live_translate_probe.py --limit 3 --output .analysis-tmp\gemini_live_translate_probe_live_3.jsonl --timeout-seconds 35 --receive-idle-seconds 5
```

API key lookup order:

1. `GEMINI_API_KEY`
2. `GOOGLE_API_KEY`

Current target language code: `zh-Hant`.

The script expects each `source_chunks[].audio_path` to be 16 kHz mono 16-bit PCM
wav, matching Gemini Live Translate input requirements.

## Implementation Notes

- Model: `gemini-3.5-live-translate-preview`.
- SDK: local venv `google-genai` was upgraded from `2.6.0` to `2.8.0`.
- Reason: `2.6.0` exposed old `StreamTranslationConfig`, which serialized to an
  API-rejected field. `2.8.0` supports the official `TranslationConfig`.
- Transcription config must not set `languageCodes` in Gemini Developer API mode.
- Audio is sent in 100 ms chunks by default, with realtime pacing.
- Live Translate behaves as a continuous stream, so the probe uses timeout/idle
  cutoff rather than expecting a clean terminal turn for offline clips.

## Output Schema

Each JSONL row keeps:

- candidate metadata: `sample_id`, bucket, source ids, source chunk usages
- current live_translate baseline: `source_text`, `baseline_target_text`
- audio diagnostics: chunk paths, duration, sent PCM bytes
- Gemini result: input transcription, output transcription, output audio bytes
- runtime diagnostics: sent chunks, received messages, timeout status

This is enough for Phase 0 human labeling to compare Gemini against current
runtime output without losing source evidence.

## Initial Result

Command:

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\gemini_live_translate_probe.py --limit 3 --output .analysis-tmp\gemini_live_translate_probe_live_3.jsonl --timeout-seconds 35 --receive-idle-seconds 5
```

Summary:

| sample | bucket | status | audio | latency | note |
| --- | --- | --- | ---: | ---: | --- |
| S001 | forced_cut | partial | 8.30s | 37.7s | Output was readable zh-Hant; needs human quality judgment. |
| S002 | forced_cut | partial | 10.40s | 35.8s | Output followed audible English/clip-like source, not obviously aligned with baseline target. |
| S003 | forced_cut | partial | 13.42s | 36.3s | Output was readable zh-Hant; source attribution and term quality need judgment. |

All three sessions returned translated output/audio but ended by probe timeout,
not by a clean Live API terminal event. Treat `partial` as usable probe output,
not as production-ready completion semantics.

## Early Assessment

Gemini Live Translate is worth evaluating as an external candidate, but it should
not replace live_translate yet.

Reasons:

- It can produce usable zh-Hant directly from audio, which may bypass some STT
  and forced-cut failure modes.
- It does not provide live_translate's `source_chunk_usages`,
  carry-forward evidence, speaker policy decisions, glossary/profile controls,
  or runtime diagnostics by itself.
- In mixed-source audio, it may translate whichever speech is most salient. That
  is useful evidence for speaker/source errors, but not a solution to
  host-primary routing.
- Continuous stream completion semantics are not a drop-in fit for subtitle
  events; offline probing needs an explicit timeout/idle policy.
- Latency in this first offline probe is high because the script waits for
  translated audio/transcription and then idles out. This must be measured
  separately from real live streaming latency.

## Next Step

Run a small stratified probe, not a broad replacement test:

- 5 host-speech samples
- 5 host-over-clip or wrong-speaker-risk samples
- 5 host-silent clip/game samples
- 5 low-confidence STT or forced-cut samples

Judge each row with the same host-primary Phase 0 rules:

- source correctness
- zh-Hant subtitle usability
- profile/glossary regressions
- whether Gemini rescues STT/cut failures
- whether it worsens speaker attribution/debuggability

Only after that should it be considered for a constrained role:

- benchmark oracle
- fallback for low-confidence STT segments
- offline QA signal
- or, if it wins on quality and latency, a separate live mode
