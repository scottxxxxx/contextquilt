"""
App schema manifest validator.

Validates a JSON manifest submitted via POST /v1/apps/{app_id}/schema
against structural rules before the registration endpoint writes it.

Structural validation only — does NOT check that the manifest
produces good extraction output. Sample-data validation is deferred
to a future version per the design doc.
"""

from typing import Any, Dict, List, Optional, Tuple

VALID_FACETS = {
    "Attribute", "Affinity", "Intention",
    "Constraint", "Connection", "Episode",
}

VALID_PERMANENCE_CLASSES = {
    "permanent", "decade", "year", "quarter",
    "month", "week", "day",
}

VALID_CONNECTION_ROLES = {
    "parent", "depends_on", "informs",
}

SUPPORTED_FACET_ENUM_VERSIONS = {1}

TOP_LEVEL_REQUIRED = {"app_id", "version", "facet_enum_version", "patch_types", "connection_labels"}
TOP_LEVEL_OPTIONAL = {
    "display_name", "description", "design_principles",
    "origin_types", "entity_types",
    "extraction_prompt_guidance", "extraction_prompt_override",
}

PATCH_TYPE_REQUIRED = {"domain_type", "facet", "permanence", "display_name", "description", "value_shape"}
PATCH_TYPE_OPTIONAL = {"completable", "project_scoped", "self_only", "extraction_rules"}

LABEL_REQUIRED = {"label", "role", "from_types", "to_types", "description"}

ENTITY_TYPE_REQUIRED = {"entity_type", "display_name", "description"}
ENTITY_TYPE_OPTIONAL = {"indexed", "extraction_rules"}


def validate_manifest(manifest: Dict[str, Any], app_id: str) -> Tuple[bool, List[str]]:
    """
    Validate a manifest. Returns (is_valid, errors).

    The app_id parameter is the URL path app_id; if the manifest's
    app_id field doesn't match, that's an error.
    """
    errors: List[str] = []

    if not isinstance(manifest, dict):
        return False, ["Manifest must be a JSON object."]

    # Top-level structure
    errors.extend(_check_required_keys("manifest", manifest, TOP_LEVEL_REQUIRED))
    errors.extend(_check_unknown_keys("manifest", manifest, TOP_LEVEL_REQUIRED | TOP_LEVEL_OPTIONAL))

    if manifest.get("app_id") != app_id:
        errors.append(
            f"Manifest app_id ({manifest.get('app_id')!r}) does not match URL app_id ({app_id!r})."
        )

    if not isinstance(manifest.get("version"), int) or manifest.get("version", 0) < 1:
        errors.append("Manifest version must be a positive integer.")

    facet_v = manifest.get("facet_enum_version")
    if facet_v not in SUPPORTED_FACET_ENUM_VERSIONS:
        errors.append(
            f"Manifest facet_enum_version must be one of {sorted(SUPPORTED_FACET_ENUM_VERSIONS)}; got {facet_v!r}."
        )

    # Patch types
    patch_types = manifest.get("patch_types")
    if not isinstance(patch_types, list) or not patch_types:
        errors.append("Manifest patch_types must be a non-empty array.")
        patch_types = []

    declared_types: set = set()
    for idx, pt in enumerate(patch_types):
        type_errors = _validate_patch_type(pt, idx)
        errors.extend(type_errors)
        if isinstance(pt, dict) and isinstance(pt.get("domain_type"), str):
            if pt["domain_type"] in declared_types:
                errors.append(f"Duplicate patch_type.domain_type: {pt['domain_type']!r}.")
            declared_types.add(pt["domain_type"])

    # Connection labels
    labels = manifest.get("connection_labels")
    if not isinstance(labels, list) or not labels:
        errors.append("Manifest connection_labels must be a non-empty array.")
        labels = []

    declared_labels: set = set()
    for idx, lb in enumerate(labels):
        label_errors = _validate_label(lb, idx, declared_types)
        errors.extend(label_errors)
        if isinstance(lb, dict) and isinstance(lb.get("label"), str):
            if lb["label"] in declared_labels:
                errors.append(f"Duplicate connection_label.label: {lb['label']!r}.")
            declared_labels.add(lb["label"])

    # Origin types (optional array of strings)
    origin_types = manifest.get("origin_types")
    if origin_types is not None:
        if not isinstance(origin_types, list) or not all(isinstance(o, str) and o for o in origin_types):
            errors.append("Manifest origin_types must be an array of non-empty strings when provided.")

    # Entity types (optional array of entity type declarations)
    entity_types = manifest.get("entity_types")
    if entity_types is not None:
        if not isinstance(entity_types, list):
            errors.append("Manifest entity_types must be an array when provided.")
        else:
            declared_entities: set = set()
            for idx, et in enumerate(entity_types):
                errors.extend(_validate_entity_type(et, idx))
                if isinstance(et, dict) and isinstance(et.get("entity_type"), str):
                    if et["entity_type"] in declared_entities:
                        errors.append(
                            f"Duplicate entity_types.entity_type: {et['entity_type']!r}."
                        )
                    declared_entities.add(et["entity_type"])

    # Extraction prompt override (optional, exclusive with guidance-based generation)
    override = manifest.get("extraction_prompt_override")
    if override is not None and not isinstance(override, str):
        errors.append("extraction_prompt_override must be a string when provided.")

    return (len(errors) == 0), errors


def _validate_patch_type(pt: Any, idx: int) -> List[str]:
    errors: List[str] = []
    prefix = f"patch_types[{idx}]"

    if not isinstance(pt, dict):
        return [f"{prefix} must be an object."]

    errors.extend(_check_required_keys(prefix, pt, PATCH_TYPE_REQUIRED))
    errors.extend(_check_unknown_keys(prefix, pt, PATCH_TYPE_REQUIRED | PATCH_TYPE_OPTIONAL))

    facet = pt.get("facet")
    if facet not in VALID_FACETS:
        errors.append(
            f"{prefix}.facet must be one of {sorted(VALID_FACETS)}; got {facet!r}."
        )

    permanence = pt.get("permanence")
    if permanence not in VALID_PERMANENCE_CLASSES:
        errors.append(
            f"{prefix}.permanence must be one of {sorted(VALID_PERMANENCE_CLASSES)}; got {permanence!r}."
        )

    domain_type = pt.get("domain_type")
    if not isinstance(domain_type, str) or not domain_type.strip():
        errors.append(f"{prefix}.domain_type must be a non-empty string.")

    value_shape = pt.get("value_shape")
    if not isinstance(value_shape, dict) or not value_shape:
        errors.append(f"{prefix}.value_shape must be a non-empty object.")

    # Optional typed fields
    for bool_field in ("completable", "project_scoped", "self_only"):
        if bool_field in pt and not isinstance(pt[bool_field], bool):
            errors.append(f"{prefix}.{bool_field} must be a boolean.")

    return errors


def _validate_label(lb: Any, idx: int, declared_types: set) -> List[str]:
    errors: List[str] = []
    prefix = f"connection_labels[{idx}]"

    if not isinstance(lb, dict):
        return [f"{prefix} must be an object."]

    errors.extend(_check_required_keys(prefix, lb, LABEL_REQUIRED))

    role = lb.get("role")
    if role not in VALID_CONNECTION_ROLES:
        errors.append(
            f"{prefix}.role must be one of {sorted(VALID_CONNECTION_ROLES)}; got {role!r}."
        )

    for endpoint in ("from_types", "to_types"):
        value = lb.get(endpoint)
        if not isinstance(value, list) or not value:
            errors.append(f"{prefix}.{endpoint} must be a non-empty array.")
            continue
        if not all(isinstance(t, str) for t in value):
            errors.append(f"{prefix}.{endpoint} must contain only strings.")
            continue
        # Check referential integrity — types referenced by labels must exist in patch_types.
        # Exception: nothing — every type referenced must be declared.
        missing = [t for t in value if t not in declared_types]
        if missing:
            errors.append(
                f"{prefix}.{endpoint} references undeclared patch_types: {missing}. "
                f"Declared types: {sorted(declared_types)}."
            )

    label = lb.get("label")
    if not isinstance(label, str) or not label.strip():
        errors.append(f"{prefix}.label must be a non-empty string.")

    return errors


def _validate_entity_type(et: Any, idx: int) -> List[str]:
    errors: List[str] = []
    prefix = f"entity_types[{idx}]"

    if not isinstance(et, dict):
        return [f"{prefix} must be an object."]

    errors.extend(_check_required_keys(prefix, et, ENTITY_TYPE_REQUIRED))
    errors.extend(_check_unknown_keys(prefix, et, ENTITY_TYPE_REQUIRED | ENTITY_TYPE_OPTIONAL))

    entity_type = et.get("entity_type")
    if not isinstance(entity_type, str) or not entity_type.strip():
        errors.append(f"{prefix}.entity_type must be a non-empty string.")

    if "indexed" in et and not isinstance(et["indexed"], bool):
        errors.append(f"{prefix}.indexed must be a boolean.")

    return errors


def _check_required_keys(prefix: str, obj: Dict[str, Any], required: set) -> List[str]:
    missing = [k for k in required if k not in obj]
    if missing:
        return [f"{prefix} is missing required keys: {sorted(missing)}."]
    return []


def _check_unknown_keys(prefix: str, obj: Dict[str, Any], allowed: set) -> List[str]:
    unknown = [k for k in obj.keys() if k not in allowed]
    if unknown:
        return [f"{prefix} has unknown keys: {sorted(unknown)}."]
    return []
