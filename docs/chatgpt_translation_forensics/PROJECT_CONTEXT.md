# live_translate Translation-Forensics Context

## Purpose and scope

`live_translate` turns Korean livestream audio into ordered Traditional Chinese
subtitles on Windows. This document describes current production structure so a
reviewer can investigate an uploaded runtime bundle. It does not contain
evaluation answers, known-bad sentences, historical verdicts, or preferred
forensic conclusions.

## Current live graph

```text
Windows audio capture
  -> VAD and bounded audio chunks
  -> batch STT
  -> sentence assembly
  -> optional one-shot provisional translation
  -> concurrent final translation workers
  -> in-order publication coordinator
  -> subtitle overlay
```

The default STT role is ElevenLabs Scribe v2. Groq is a same-chunk fallback for
provider failure; SenseVoice is available only when explicitly configured.
The ordinary live translation route is DeepSeek Flash, followed when necessary
by OpenRouter Qwen, DeepL, and Groq. Runtime evidence, rather than this default
list, determines which route actually handled a particular item.

Translation workers may finish concurrently, but final publication is ordered
by sequence. Audio, text, and subtitle queues favor freshness; the sentence
queue drops only its oldest item when bounded capacity is exceeded.

## Profile and context state

Profile identity is generation-scoped. A configured source profile, optional
scene-confirmed content profile, and mode together determine an effective
profile. Auto mode may update content identity from validated scene evidence;
manual mode locks the effective profile. Resolver observations and activation
are separate facts.

STT glossary construction, sentence boundaries, provisional fingerprints,
translation requests, cache identity, and history cohorts consume an immutable
`ProfileSnapshot`. Work already in flight finishes with its captured snapshot;
a later generation does not retroactively change it.

Scene-derived activity is context metadata. It never becomes spoken source
text. A profile or activity hint can guide interpretation but cannot establish
unsupported speaker meaning.

## Translation processing

Before a provider call, production applies source policy and deterministic
source normalization, resolves source-grounded canonical obligations, freezes
unknown-name and narrow terminology mappings, and builds one effective request.
Primary and fallback providers consume that frozen contract.

After a provider returns, one finalization owner restores protected spans,
applies source-aware deterministic corrections, evaluates required occurrences,
runs script/meta/content guards, and either produces a fail-closed result or a
publishable translation. Successful primary, fallback, cache, and exact
provisional-promotion paths converge on this finalization/publication boundary.

History and cache are scoped by the effective request identity. A cache result
or prior history item is evidence of reuse, not independent proof that its text
is semantically correct.

## Provisional and final subtitles

A provisional candidate is one-shot. Promotion is allowed only when its frozen
fingerprint matches the final source/profile/activity/context contract. A
mismatch closes the candidate and uses the ordinary final route. Final revision
and display evidence exist only where runtime fields/events persisted them.

## Evidence and offline tools

Runtime JSONL is the primary persisted evidence. The portable ChatGPT bundle
exports every persisted record for one `run_id` after secret/privacy
sanitization, plus derived indexes and optional retained WAVs.

The runtime analyzer summarizes mechanical telemetry. The semantic-review
HARNESS can rank translations for investigation. Neither analyzer output nor a
model review is ground truth. They are aids for locating evidence.

## Knowledge boundary

Safe persistent context includes current architecture, interfaces, general
contracts, runtime schemas, and evidence limitations. Evaluation-leaking
material includes benchmark sources and expected outputs, known-good/known-bad
labels, reviewer calibration labels, historical sentence-level findings,
case-specific root causes, and prior run verdicts. Evaluation-leaking material
must remain outside persistent Project Sources and may be introduced only after
an initial blind review if the owner explicitly requests a Phase 2 comparison.
