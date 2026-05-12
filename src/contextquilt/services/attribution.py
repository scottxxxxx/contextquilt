"""User-attribution soft-signal validator.

Validates the `user_attribution_hint` payload that arrives on the
`metadata` field of /v1/memory (forwarded by the calling app's
identity layer — today: ShoulderSurf via GhostPour).

Wire shape per the v1 spec ack'd 2026-05-12:

    {
      "speaker_label": "Speaker 3",
      "confidence": 0.42,
      "confidence_basis": "combined",
      "secondary_candidate": {        # optional
        "speaker_label": "Speaker 5",
        "confidence": 0.31
      }
    }

Validation is graceful: malformed hints are logged and dropped, the
extraction continues as if the field were absent. The producer (SS
via GP) sees the warning in worker logs and can correct on their side
without breaking the user-facing ingestion path.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

CONFIDENCE_BASIS_VALUES = frozenset(
    {
        "enrolled_similarity",
        "cumulative_seconds",
        "embedding_consistency",
        "combined",
    }
)


def _validate_candidate(
    candidate: Any, *, label_field: str, conf_field: str
) -> tuple[str, float] | None:
    """Validate a {speaker_label, confidence} sub-object.

    Returns (label, confidence) on success, None on any validation
    failure (logging a warning for each).
    """
    if not isinstance(candidate, dict):
        logger.warning(
            "attribution_hint_invalid",
            reason="candidate not a dict",
            field=label_field,
        )
        return None

    label = candidate.get("speaker_label")
    if not isinstance(label, str) or not label.strip():
        logger.warning(
            "attribution_hint_invalid",
            reason="speaker_label missing or empty",
            field=label_field,
        )
        return None

    conf = candidate.get("confidence")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool):
        logger.warning(
            "attribution_hint_invalid",
            reason="confidence not a number",
            field=conf_field,
            value=conf,
        )
        return None
    conf = float(conf)
    if not (0.0 <= conf <= 1.0):
        logger.warning(
            "attribution_hint_invalid",
            reason="confidence out of range",
            field=conf_field,
            value=conf,
        )
        return None

    return label.strip(), conf


def validate_user_attribution_hint(hint: Any) -> dict[str, Any] | None:
    """Validate and normalize a user_attribution_hint payload.

    Returns the cleaned dict on success, or None when:
    - the hint is missing or explicitly null
    - any required field is missing or malformed
    - confidence values are out of range
    - confidence_basis is not in the enum
    - secondary_candidate.confidence > primary.confidence

    The cleaned dict has the same shape as the input plus normalized
    types (confidence cast to float, speaker_label stripped). Always
    safe to drop the field on validation failure — see module
    docstring on graceful-degrade semantics.
    """
    if hint is None:
        return None
    if not isinstance(hint, dict):
        logger.warning(
            "attribution_hint_invalid",
            reason="not a dict",
            value=type(hint).__name__,
        )
        return None

    primary = _validate_candidate(
        hint, label_field="speaker_label", conf_field="confidence"
    )
    if primary is None:
        return None
    primary_label, primary_conf = primary

    basis = hint.get("confidence_basis")
    if not isinstance(basis, str) or basis not in CONFIDENCE_BASIS_VALUES:
        logger.warning(
            "attribution_hint_invalid",
            reason="confidence_basis missing or unknown",
            value=basis,
        )
        return None

    secondary_raw = hint.get("secondary_candidate")
    secondary_label: str | None = None
    secondary_conf: float | None = None
    if secondary_raw is not None:
        secondary = _validate_candidate(
            secondary_raw,
            label_field="secondary_candidate.speaker_label",
            conf_field="secondary_candidate.confidence",
        )
        if secondary is None:
            return None
        secondary_label, secondary_conf = secondary
        if secondary_conf > primary_conf:
            logger.warning(
                "attribution_hint_invalid",
                reason="secondary confidence exceeds primary",
                primary=primary_conf,
                secondary=secondary_conf,
            )
            return None

    cleaned: dict[str, Any] = {
        "speaker_label": primary_label,
        "confidence": primary_conf,
        "confidence_basis": basis,
    }
    if secondary_label is not None:
        cleaned["secondary_candidate"] = {
            "speaker_label": secondary_label,
            "confidence": secondary_conf,
        }
    return cleaned
