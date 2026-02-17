import json
import uuid
import datetime
import re

def repair_and_parse_json(llm_raw_output):
    """
    1. Re-attaches missing opening brace (from pre-fill strategy).
    2. Strips Markdown blocks.
    3. Returns dict.
    """
    content = llm_raw_output.strip()
    
    # Strip markdown
    if "```" in content:
        match = re.search(r"```(?:json)?(.*?)```", content, re.DOTALL)
        if match:
            content = match.group(1).strip()

    # Repair Pre-fill
    if not content.startswith("{"):
        content = "{" + content

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        print("[ERROR] Failed to parse JSON.")
        return {}

def prune_invalid_values(data):
    """Recursively removes empty, null, 'unknown', or 'not specified' values."""
    if not isinstance(data, dict):
        return data

    clean_data = {}
    for k, v in data.items():
        if isinstance(v, dict):
            nested = prune_invalid_values(v)
            if nested:
                clean_data[k] = nested
        elif isinstance(v, list):
            clean_data[k] = v
        else:
            if v is None: continue
            if isinstance(v, str) and v.lower().strip() in ["", "not specified", "unknown", "none", "n/a"]:
                continue
            clean_data[k] = v
    return clean_data

def transform_to_patches(raw_output, subject_key="user_unknown"):
    """
    Transforms Stitcher JSON into flat ContextPatch objects for Vector DB.
    """
    data = repair_and_parse_json(raw_output)
    clean_data = prune_invalid_values(data)
    
    patches = []
    bucket_map = {
        "identity_facts": "identity",
        "preference_facts": "preference",
        "task_facts": "experience",
        "constraints_facts": "experience"
    }

    # Default Meta
    meta = data.get("meta", {})
    confidence = meta.get("confidence_overall", 1.0)

    for bucket, db_type in bucket_map.items():
        if bucket in clean_data:
            for key, value in clean_data[bucket].items():
                patches.append({
                    "patch_id": str(uuid.uuid4()),
                    "subject_key": subject_key,
                    "patch_type": db_type,
                    "patch_name": key,
                    "value": value,
                    "origin_mode": "derived",
                    "source_prompt": "stitcher_v2.1",
                    "confidence": confidence,
                    "created_at": datetime.datetime.utcnow().isoformat() + "Z"
                })
    return patches
