"""Stable, sanitized manifests for reconstructing provider request causality."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence


REQUEST_CONTRACT_SCHEMA_VERSION = 1
ADJUDICATION_POLICY_VERSION = "candidate-adjudication-v2"
STT_REQUEST_CONTRACT_SCHEMA_VERSION = 1


def sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def stable_identity(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def message_manifest(
    messages: Sequence[tuple[str, str]],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "index": index,
            "role": role,
            "content": content,
            "content_sha256": sha256_text(content),
            "char_count": len(content),
        }
        for index, (role, content) in enumerate(messages)
    )


def history_manifest(
    history: Sequence[tuple[str, str]] | None,
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "index": index,
            "source": source,
            "target": target,
            "source_sha256": sha256_text(source),
            "target_sha256": sha256_text(target),
            "pair_sha256": stable_identity(
                {"source": source, "target": target}
            ),
        }
        for index, (source, target) in enumerate(history or ())
    )


def artifact_hashes(
    project_root: Path,
    relative_paths: Iterable[str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in relative_paths:
        path = project_root / relative
        try:
            result[relative.replace("\\", "/")] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        except OSError:
            result[relative.replace("\\", "/")] = ""
    return result
