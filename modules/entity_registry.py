"""Reviewed, scope-aware entity knowledge and deterministic consumer views."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata
from typing import Any


_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "entity_registry.json"
_PROFILE_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "streamer_profiles.json"
_ID_RE = re.compile(r"^[a-z0-9_]{1,96}$")
ALIAS_SCOPES = frozenset({
    "identity_visible", "identity_ocr", "stt", "translation_source", "display"
})


def normalize_alias(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


@dataclass(frozen=True)
class EntityAlias:
    value: str
    scopes: frozenset[str]
    requires_context: bool = False
    orders: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class EntityIdentityBinding:
    profile_id: str
    marker_id: str
    strength: str
    kind: str


@dataclass(frozen=True)
class EntityTranslationRule:
    scope: str
    wrong_forms: tuple[str, ...]
    publication_policy: str
    condition_id: str
    activation_policy: str
    repair_requires_name_context: bool


@dataclass(frozen=True)
class ReviewedEntity:
    entity_id: str
    entity_type: str
    canonical_source: str
    canonical_target: str
    profile_ids: tuple[str, ...]
    aliases: tuple[EntityAlias, ...]
    identity: EntityIdentityBinding | None = None
    translation: EntityTranslationRule | None = None
    provenance: str = "reviewed_existing_data"
    review_status: str = "reviewed"

    def aliases_for(self, scope: str) -> tuple[str, ...]:
        indexed = [
            (dict(alias.orders).get(scope, index), index, alias.value)
            for index, alias in enumerate(self.aliases) if scope in alias.scopes
        ]
        return tuple(value for _, _, value in sorted(indexed))


@dataclass(frozen=True)
class EntityRegistry:
    schema_version: int
    identity: str
    entities: tuple[ReviewedEntity, ...]

    def entity(self, entity_id: str) -> ReviewedEntity | None:
        return next((row for row in self.entities if row.entity_id == entity_id), None)

    def relevant(self, profile_id: str) -> tuple[ReviewedEntity, ...]:
        return tuple(row for row in self.entities if profile_id in row.profile_ids)

    def exact_lookup(self, value: str, scope: str, profile_id: str = "") -> ReviewedEntity | None:
        key = normalize_alias(value)
        matches = [
            row for row in self.entities
            if (not profile_id or profile_id in row.profile_ids)
            and any(normalize_alias(alias.value) == key and scope in alias.scopes for alias in row.aliases)
        ]
        return matches[0] if len(matches) == 1 else None

    def referenced_alias(self, entity_id: str, value: str, scope: str, profile_id: str) -> str:
        entity = self.entity(entity_id)
        if entity is None or profile_id not in entity.profile_ids:
            raise ValueError(f"invalid entity reference: {entity_id!r}")
        matches = [alias.value for alias in entity.aliases if scope in alias.scopes and alias.value == value]
        if len(matches) != 1:
            raise ValueError(
                f"entity {entity_id!r} has no unique {scope} alias {value!r}"
            )
        return matches[0]


def _strings(value: Any, field: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{field} must be a list of strings")
    result = tuple(value)
    if nonempty and (not result or any(not x.strip() for x in result)):
        raise ValueError(f"{field} must contain non-empty strings")
    return result


def load_entity_registry(path: Path = _DATA_PATH, *, known_profiles: frozenset[str] | None = None) -> EntityRegistry:
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
        raise ValueError("entity registry must contain an entities list")
    version = data.get("schema_version")
    if not isinstance(version, int) or version < 1:
        raise ValueError("entity registry schema_version must be a positive integer")
    ids: set[str] = set()
    scoped_owners: dict[tuple[str, str, str], tuple[str, str]] = {}
    marker_ids: set[str] = set()
    entities: list[ReviewedEntity] = []
    for index, raw_entity in enumerate(data["entities"]):
        field = f"entities[{index}]"
        if not isinstance(raw_entity, dict):
            raise ValueError(f"{field} must be an object")
        entity_id = raw_entity.get("entity_id")
        if not isinstance(entity_id, str) or not _ID_RE.fullmatch(entity_id) or entity_id in ids:
            raise ValueError(f"invalid or duplicate entity_id: {entity_id!r}")
        ids.add(entity_id)
        entity_type = raw_entity.get("entity_type")
        canonical_source = raw_entity.get("canonical_source")
        canonical_target = raw_entity.get("canonical_target")
        if not all(isinstance(x, str) and x.strip() for x in (entity_type, canonical_source, canonical_target)):
            raise ValueError(f"{field} requires non-empty type and canonical names")
        profiles = _strings(raw_entity.get("profile_ids"), f"{field}.profile_ids", nonempty=True)
        if known_profiles is not None:
            missing = set(profiles) - known_profiles
            if missing:
                raise ValueError(f"{field} references unknown profiles: {sorted(missing)}")
        raw_aliases = raw_entity.get("aliases")
        if not isinstance(raw_aliases, list) or not raw_aliases:
            raise ValueError(f"{field}.aliases must be a non-empty list")
        aliases: list[EntityAlias] = []
        for alias_index, raw_alias in enumerate(raw_aliases):
            alias_field = f"{field}.aliases[{alias_index}]"
            if not isinstance(raw_alias, dict) or not isinstance(raw_alias.get("value"), str) or not raw_alias["value"].strip():
                raise ValueError(f"{alias_field} requires a non-empty value")
            scopes = frozenset(_strings(raw_alias.get("scopes"), f"{alias_field}.scopes", nonempty=True))
            unknown = scopes - ALIAS_SCOPES
            if unknown:
                raise ValueError(f"{alias_field} has unknown scopes: {sorted(unknown)}")
            value = raw_alias["value"].strip()
            requires_context = raw_alias.get("requires_context", False)
            if not isinstance(requires_context, bool):
                raise ValueError(f"{alias_field}.requires_context must be a boolean")
            if requires_context and "translation_source" not in scopes:
                raise ValueError(
                    f"{alias_field}.requires_context requires translation_source scope"
                )
            raw_order = raw_alias.get("order", {})
            if (
                not isinstance(raw_order, dict)
                or not all(
                    isinstance(key, str) and key in scopes
                    and isinstance(position, int) and position >= 0
                    for key, position in raw_order.items()
                )
            ):
                raise ValueError(f"{alias_field}.order must map alias scopes to positions")
            aliases.append(EntityAlias(
                value, scopes, requires_context, tuple(sorted(raw_order.items()))
            ))
            for scope in scopes:
                # Translation aliases are profile scoped; identity/STT aliases must
                # also be unambiguous inside every profile that can consume them.
                for profile in profiles:
                    key = (profile, scope, normalize_alias(value))
                    owner = scoped_owners.get(key)
                    candidate = (entity_id, canonical_target)
                    if owner is not None and owner != candidate:
                        raise ValueError(f"alias collision in {profile}/{scope}: {value!r}")
                    scoped_owners[key] = candidate
        for scope in ALIAS_SCOPES:
            positions = [dict(alias.orders)[scope] for alias in aliases if scope in dict(alias.orders)]
            if len(positions) != len(set(positions)):
                raise ValueError(f"{field} has duplicate {scope} alias order")
        identity = None
        raw_identity = raw_entity.get("identity")
        if raw_identity is not None:
            if not isinstance(raw_identity, dict):
                raise ValueError(f"{field}.identity must be an object")
            marker_id = raw_identity.get("marker_id")
            profile_id = raw_identity.get("profile_id")
            strength = raw_identity.get("strength", "strong")
            kind = raw_identity.get("kind", "member_name")
            if not isinstance(marker_id, str) or not _ID_RE.fullmatch(marker_id) or marker_id in marker_ids:
                raise ValueError(f"invalid or duplicate marker_id: {marker_id!r}")
            if profile_id not in profiles or strength not in {"strong", "medium", "weak"} or not isinstance(kind, str):
                raise ValueError(f"{field}.identity has invalid profile/strength/kind")
            if not any("identity_visible" in alias.scopes for alias in aliases):
                raise ValueError(f"{field}.identity lacks visible activation data")
            marker_ids.add(marker_id)
            identity = EntityIdentityBinding(profile_id, marker_id, strength, kind)
        translation = None
        raw_translation = raw_entity.get("translation")
        if raw_translation is not None:
            if not isinstance(raw_translation, dict):
                raise ValueError(f"{field}.translation must be an object")
            scope = raw_translation.get("scope")
            policy = raw_translation.get("publication_policy", "repair_only")
            activation = raw_translation.get("activation_policy", "exact_alias")
            condition = raw_translation.get("condition_id", "always")
            repair_context = raw_translation.get("repair_requires_name_context", False)
            if scope != "__shared__" and scope not in profiles:
                raise ValueError(f"{field}.translation has invalid scope")
            if policy not in {"repair_only", "required"} or activation not in {"exact_alias", "name_context_required"}:
                raise ValueError(f"{field}.translation has invalid policy")
            if not isinstance(condition, str) or not isinstance(repair_context, bool):
                raise ValueError(f"{field}.translation has invalid condition fields")
            if not any("translation_source" in alias.scopes for alias in aliases):
                raise ValueError(f"{field}.translation lacks source activation data")
            if any(alias.requires_context for alias in aliases) and activation != "name_context_required":
                raise ValueError(f"{field} loses required alias context policy")
            translation = EntityTranslationRule(
                scope, _strings(raw_translation.get("wrong_forms"), f"{field}.translation.wrong_forms"),
                policy, condition, activation, repair_context,
            )
        entities.append(ReviewedEntity(
            entity_id, entity_type, canonical_source, canonical_target, profiles,
            tuple(aliases), identity, translation,
            str(raw_entity.get("provenance", "reviewed_existing_data")),
            str(raw_entity.get("review_status", "reviewed")),
        ))
    return EntityRegistry(version, f"entities:{version}:{sha256(raw).hexdigest()[:16]}", tuple(entities))


def load_production_entity_registry(
    entity_path: Path = _DATA_PATH,
    profile_path: Path = _PROFILE_DATA_PATH,
) -> EntityRegistry:
    profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
    rows = profile_data.get("profiles") if isinstance(profile_data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("streamer profile data must contain a profiles list")
    profile_ids = frozenset(
        row.get("profile_id") for row in rows
        if isinstance(row, dict) and isinstance(row.get("profile_id"), str)
    )
    return load_entity_registry(entity_path, known_profiles=profile_ids)


ENTITY_REGISTRY = load_production_entity_registry()
