"""Lossless ordered source genres and an independent classification overlay."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence


PROTECTED_FIELDS = frozenset({
    "entity_type", "entity_id", "genres_raw", "genres_json", "primary_genre",
    "genre_count", "secondary_genres", *(f"genre_{n}" for n in range(2, 9)),
    "content_hash", "source_url", "source_system", "verification_status",
    "first_seen", "last_seen", "last_checked", "active_status", "raw_source_ref",
})


def split_genres(raw: str | None) -> list[str]:
    """Split slash/comma/semicolon outside brackets; keep order and duplicates.

    Parenthetical source qualifiers remain part of a token. This deliberately
    does not guess how to split 'and', ampersands, or ambiguous prose. Such
    strings can be mapped in the editable Genre Map without altering the source.
    """
    if not raw:
        return []
    output, token, depth = [], [], 0
    for char in raw:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char in "/,;" and depth == 0:
            value = "".join(token).strip()
            if value:
                output.append(value)
            token = []
        else:
            token.append(char)
    value = "".join(token).strip()
    if value:
        output.append(value)
    return output


def genre_fields(raw: str | None, ordered: Sequence[str] | str | None = None) -> dict:
    """Return every ordered source field, preserving the raw string exactly."""
    raw = "" if raw is None else str(raw)
    if isinstance(ordered, str):
        try:
            ordered = json.loads(ordered)
        except (TypeError, ValueError):
            ordered = None
    genres = list(ordered) if isinstance(ordered, (list, tuple)) else split_genres(raw)
    genres = [str(value) for value in genres]
    result = {
        "genres_raw": raw,
        "genres_json": genres,
        "primary_genre": genres[0] if genres else "",
        "genre_count": len(genres),
        "secondary_genres": genres[1:],
    }
    result.update({f"genre_{n}": genres[n - 1] if len(genres) >= n else "" for n in range(2, 9)})
    return result


def classify(fields: Mapping, mappings: Mapping[str, Mapping] | None = None, taxonomy_version: str = "1") -> dict:
    """Map the first genre by default; secondary genres become searchable tags."""
    mappings = mappings or {}
    primary = fields.get("primary_genre", "")
    mapping = mappings.get(primary)
    tags = []
    for genre in fields.get("secondary_genres", []):
        mapped = mappings.get(genre, {})
        tag = mapped.get("ecommerce_classification") or genre
        if tag and tag not in tags:
            tags.append(tag)
    extra = mapping.get("secondary_tags", []) if mapping else []
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = [tag.strip() for tag in extra.split(";") if tag.strip()]
    for tag in extra:
        if tag and tag not in tags:
            tags.append(tag)
    confidence = float(mapping.get("confidence", 0.0)) if mapping else 0.0
    return {
        "ecommerce_primary_class": (mapping.get("ecommerce_classification") or primary) if mapping else primary,
        "ecommerce_secondary_tags": tags,
        "classification_status": mapping.get("review_status", "needs_review") if mapping else "needs_review",
        "classification_confidence": min(1.0, max(0.0, confidence)),
        "taxonomy_version": str(mapping.get("taxonomy_version", taxonomy_version)) if mapping else taxonomy_version,
    }


def apply_overrides(record: dict, overrides: Sequence[Mapping]) -> dict:
    result = dict(record)
    applied = []
    for override in overrides:
        field = override["field"]
        if field in PROTECTED_FIELDS:
            continue
        result[field] = override.get("override_value", "")
        applied.append(field)
    if applied:
        result["override_fields"] = applied
        if "classification_status" not in applied:
            result["classification_status"] = "overridden"
    return result
