# Blind Forensic Method

Perform Phase 1 without benchmark answers, historical case labels, prior run
conclusions, or semantic-review verdicts treated as truth.

## Phase 1: independent runtime analysis

1. **Establish scope.** Confirm one `run_id`, bundle schema, ordered raw parts,
   sanitization status, event counts, observed time span, and missing evidence.
2. **Build the timeline.** Track capture/STT, sentence assembly, profile/activity
   state, provider attempts, deterministic finalization, and publication.
3. **Inspect published subtitles independently.** Review every published target
   or use a transparent sampling boundary. Do not wait for the owner or HARNESS
   to nominate suspicious rows.
4. **Open a candidate finding.** State the exact fidelity concern without yet
   assigning root cause.
5. **Trace provenance.** Join the published row backward through translation,
   sentence, STT/utterance, optional audio, profile generation, context/history,
   attempts, corrections, guards, and provisional lifecycle.
6. **Test competing explanations.** Evaluate source/STT, sentence assembly,
   profile/context, translation provider, deterministic transformation,
   fallback/runtime, and publication as peers. Seek evidence that falsifies each
   explanation as well as evidence that supports it.
7. **Set the evidence boundary.** Separate observed facts, reasonable
   inferences, unavailable evidence, and unsupported possibilities.
8. **Classify and rank.** Use confirmed defect, suspicious finding, source
   uncertainty, or insufficient evidence. Rank by user-visible severity and
   confidence, not novelty.

## Ownership discipline

Do not assign an owner from surface appearance. For example, a mismatch between
audio and final subtitle could enter at STT, assembly, translation, a
deterministic transform, or publication. Only correlation evidence should
narrow the owner.

For every material finding report:

- run and stable identifiers;
- observed source and published target;
- relevant raw part/line or event ordinal;
- profile/activity generation and context identity;
- selected provider plus preceding attempts;
- deterministic changes and guards;
- publication/provisional disposition;
- competing explanations considered;
- missing evidence;
- classification, severity, confidence, and supported owner.

## Phase 2: optional comparison

Only after recording the blind Phase 1 findings may the owner supply HARNESS
rankings, historical cases, benchmark evidence, or prior conclusions. Compare
them against the frozen Phase 1 report. Do not silently rewrite the blind
findings merely to agree with Phase 2 material; explain agreements, misses, and
changes supported by newly introduced evidence.
