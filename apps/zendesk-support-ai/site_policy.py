"""Load deployment-specific routing policy from an untracked local JSON file."""
from __future__ import annotations

import json
import os
from pathlib import Path


def load_policy() -> dict[str, str]:
    path = os.environ.get("SUPPORT_AI_SITE_POLICY_FILE", "").strip()
    if not path:
        return {}
    try:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("Cannot load configured site policy") from None
    if not isinstance(policy, dict) or any(not isinstance(v, str) for v in policy.values()):
        raise RuntimeError("Site policy must be a JSON object of strings")
    required = ("special_support_area", "special_agent_tag", "special_group_name",
                "passkey_support_area", "passkey_agent_tag", "support_area_instructions")
    if any(not policy.get(key, "").strip() for key in required):
        raise RuntimeError("Configured site policy is incomplete")
    for key in ("special_support_area", "passkey_support_area"):
        if key in policy and (not policy[key].strip() or policy[key] != policy[key].strip().lower()):
            raise RuntimeError("Support area identifiers must be nonempty lowercase strings")
    return policy


POLICY = load_policy()
SPECIAL_SUPPORT_AREA = os.environ.get("SUPPORT_AI_SPECIAL_SUPPORT_AREA", POLICY.get("special_support_area", "special")).strip().lower()
PASSKEY_SUPPORT_AREA = os.environ.get("SUPPORT_AI_PASSKEY_SUPPORT_AREA", POLICY.get("passkey_support_area", "passkey")).strip().lower()
if len({"general", SPECIAL_SUPPORT_AREA, PASSKEY_SUPPORT_AREA}) != 3 or not SPECIAL_SUPPORT_AREA or not PASSKEY_SUPPORT_AREA:
    raise RuntimeError("Support area identifiers must be distinct and nonempty")
SPECIAL_AGENT_TAG = POLICY.get("special_agent_tag", "special_support")
SPECIAL_GROUP_NAME = POLICY.get("special_group_name", "Specialist")
SPECIAL_GROUP_ID = POLICY.get("special_group_id", "")
PASSKEY_AGENT_TAG = POLICY.get("passkey_agent_tag", "authentication_support")
SUPPORT_AREA_DESCRIPTION = POLICY.get("support_area_description", "Use configured support areas; choose general when uncertain")
SUPPORT_AREA_INSTRUCTIONS = POLICY.get("support_area_instructions", "不明な担当領域はsupport_area=generalとし、担当先を推測しないでください。")

SUPPORT_AREA_REASON_DESCRIPTION = POLICY.get("support_area_reason_description", "Evidence for the selected support area, or reason to choose general")
