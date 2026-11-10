# Reviewed entity registry

`data/entity_registry.json` is the mutable source of truth for reviewed HADES,
UR:L, and ISEGYE entity aliases used by identity ROI/scene matching, STT bias,
and deterministic translation name rules. Every alias declares its consumer
scope. `data/streamer_profiles.json` owns profile relevance and ordered STT
references; it no longer owns migrated alias definitions or identity markers.

`modules/entity_registry.py` validates and compiles the registry. Existing
consumer APIs remain compatibility projections through `profile_context`,
`streamer_profiles`, and `translation_corrections`.

The free-form examples and non-entity glossary prose in
`data/translation_profiles.json` remain explicit presentation data. Their
format and contextual instructions cannot be losslessly generated from entity
records, so they are not canonical alias evidence. Conditional phrase repairs
remain in `translation_corrections.json`. The unknown-name escrow and semantic
terminology placeholders were removed on 2026-11-10 (no live activation since
10/04 except one lost subtitle); an unconfirmed name that keeps recurring
should become a registry entity instead.
