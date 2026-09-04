# Owner Upload Guide

## Create the Project

Create a separate ChatGPT Project for `live_translate` translation-quality
forensics. Paste the contents of `PROJECT_INSTRUCTIONS.md` into Project
Instructions and upload the other neutral files listed below as persistent
Project Sources.

## Persistent Project Sources

Upload:

1. `PROJECT_CONTEXT.md`
2. `TRANSLATION_PIPELINE.md`
3. `TRANSLATION_CONTRACT.md`
4. `RUNTIME_EVIDENCE_GUIDE.md`
5. `FORENSIC_METHOD.md`

Keep `OWNER_UPLOAD_GUIDE.md` locally as the operating guide. It is safe but not
needed for forensic reasoning. Do not upload repository benchmark datasets,
semantic evaluation fixtures, calibration artifacts, prior forensic reports,
historical run conclusions, or correction proposals as persistent sources.

## Per-run evidence

For each blind review, upload one exported bundle's:

- `CHATGPT_PROJECT_README.md`;
- `manifest.json`;
- every ordered `runtime_events*.jsonl` part;
- `subtitles.tsv`;
- `audio_index.json`;
- `config_sanitized.json` when present, respecting its provenance warning;
- optional `audio/` WAV files when listening is needed and sharing is
  appropriate.

Ask the Project to perform and record a Phase 1 blind review before showing it
any prior analysis. Do not automatically add per-run findings to persistent
Project Sources. Start later runs from the neutral method, not accumulated
answers.

## Optional Phase 2

After the Phase 1 report is frozen, optionally upload or paste the specific
HARNESS/historical/benchmark comparison material needed for that conversation.
Label it as Phase 2 evidence. Remove it from persistent Project Sources after
the comparison so it cannot bias future runs.

## Source classification for maintainers

**Safe persistent context:** current production architecture and ownership,
generic correctness obligations, runtime/bundle schemas, evidence semantics,
forensic method, and evidence limitations.

**Evaluation-leaking material:** real case sources or targets used as expected
answers, pass/fail or known-good/known-failure labels, benchmark mappings,
reviewer calibration labels, historical sentence-level defects, case-specific
root causes, prior ranked findings, and expected future verdicts.

When updating this package, derive mechanical facts from current code and tests,
but never copy a test's case-specific translation content into these files.
Run a leakage audit before uploading revised sources.
