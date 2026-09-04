# General Translation Correctness Contract

This contract defines what to evaluate abstractly. It intentionally contains no
real benchmark sentence, expected answer, known failure, or case verdict.

## Deterministic system obligations

Where activated by the persisted source and frozen request, production must:

- preserve required source-grounded canonical occurrences;
- protect and restore exact source-grounded unknown-name spans under the current
  escrow rules rather than granting a global script allowance;
- apply narrow reviewed terminology ownership only when its source conditions
  are met;
- preserve required occurrence counts and reject missing protected obligations;
- enforce source-aware script, meta-text, and content publication guards;
- keep one frozen obligation/context mapping across provider fallback;
- prevent rejected candidates from entering subtitle, cache, or history state;
- preserve final sequence ordering;
- keep cache/history/profile generations within their recorded identity scope;
- require an exact frozen fingerprint before provisional promotion.

These obligations are mechanical. Their runtime evaluation fields can prove
that a check ran or a candidate was rejected, subject to telemetry availability.

## Open-ended model semantics

A publishable translation should:

- preserve the defensible meaning of the observed Korean source;
- preserve speaker/listener roles, direction, polarity, modality, tense, and
  referents to the extent the evidence supports them;
- preserve numbers, units, quantities, and named entities;
- avoid adding unsupported events, motives, participants, or certainty;
- avoid dropping meaning needed to understand the utterance;
- handle incomplete or ambiguous input conservatively;
- remain suitable as concise Traditional Chinese live subtitles;
- use profile, activity, and history as bounded context, never as permission to
  contradict or invent source meaning.

Fluency is not fidelity. A natural-sounding target can still change meaning.
Likewise, unusual wording is not automatically wrong if it faithfully reflects
the available source and deterministic obligations.

## Evidence-sensitive verdicts

Use four distinct outcomes:

- **Confirmed defect:** persisted evidence supports a material incorrect result
  and supports ownership at a particular layer.
- **Suspicious finding:** a material concern exists, but ownership or correctness
  is not yet adequately supported.
- **Source uncertainty:** the observed STT/sentence source may be wrong or
  ambiguous, and audio or equivalent evidence is needed.
- **Insufficient evidence:** required source, context, lifecycle, or provenance
  was not persisted.

Do not convert a diagnostic flag, reviewer opinion, or absent field directly
into a confirmed defect.
