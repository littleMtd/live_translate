# Blind Phase 1 Workflow

This document owns the current first-stage blind translation-forensics workflow.

## Purpose

Blind Phase 1 discovers user-visible translation defects without seeding the reviewer with known labels, expected root causes, or preferred fixes. It is a triage step, not production sign-off and not ground truth.

## Evidence source

1. The user runs `python main.py` while watching their own natural SOOP/CHZZK stream.
2. The exact resulting `run_id` is exported with `scripts/export_chatgpt_bundle.py`. Include retained audio when listening evidence is required.
3. Run `scripts/analyze_forensics_bundle.py` before upload. Integrity errors block blind review. Unresolved attribution gaps remain visible and must not be filled from current repository behavior.

If no fresh natural runtime exists, stop and report that evidence is missing. Do not search for streams, open videos, download media, or substitute an independently chosen source.

## ChatGPT Project boundary

The Blind translation-forensics Project is a ChatGPT Project UI workflow. Use computer use to upload or select the exact runtime bundle and start Phase 1. Repository scripts do not impersonate or replace this Project review.

Provide only:

- the exact bundle for the selected `run_id`;
- a neutral request for Phase 1 translation-forensics review;
- the instruction to distinguish observation, inference, and evidence gaps.

Do not provide:

- known failure labels or previously suspected cases;
- expected root causes or owning layers;
- proposed production changes;
- results from a different stream or run;
- offline ASR output presented as ground truth.

## Handoff to Codex

Return only the blind findings as triage leads. Codex independently verifies each retained lead against:

1. raw runtime events and request contracts;
2. retained local audio when relevant;
3. sentence, profile, provisional, fallback, cache and publication lineage;
4. current repository code.

No production change is justified by a blind label alone. Unprovable links remain `insufficient evidence`. Confirmed defects must be assigned to their actual owning layer before implementation.

## Completion record

Record the production `run_id`, bundle path, bundle integrity result, whether audio was included, Project review timestamp, and the returned blind findings. Preserve the original bundle; do not regenerate it to fit the findings.

