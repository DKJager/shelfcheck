# -*- coding: utf-8 -*-
"""Utilities for building prompts, parsing model replies and reconciling
multiple judges.

The functions operate on plain Python data structures and raise no side
effects; they are pure utilities used by the Lambda handler.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def build_messages(
    skin: Dict[str, Any],
    product_name: str,
    ingredients: str,
) -> List[Dict[str, str]]:
    """Construct the system and user messages for a judge model.

    Returns a two‑element list containing a ``system`` message and a ``user``
    message. The ingredient list is truncated to 4000 characters.
    """
    # Truncate ingredients to the required length.
    ing_trunc = ingredients[:4000]

    system_content = (
        "You are an AI model that evaluates how well a skincare product fits a user's skin condition. "
        "You will be given the user's skin analysis scores (ui_score 0‑100, where a higher score indicates "
        "healthier skin for that concern) and a product's ingredient list. "
        "Return a JSON object matching exactly this schema:\n"
        '{"verdict": "good" | "care" | "poor", '
        '"concerns": ['
        '{"concern": str, "effect": "helps" | "neutral" | "worsens", '
        '"ingredients": [str], "why": str}'
        "], "
        '"summary": str}\n'
        "The verdict represents the overall suitability: "
        "\"good\" if the product supports the user's skin, "
        "\"poor\" if it may worsen issues, "
        "and \"care\" if the result is uncertain. "
        "Use exactly one entry in \"concerns\" per skin score you were given, and use the score's key verbatim as \"concern\" "
        "(for example \"pore\", \"dark_circle_v2\"). Cite only ingredients that appear in the list. "
        "Provide ONLY the JSON object as the response."
    )

    user_content = (
        f"Product name: {product_name}\n"
        f"Ingredients (truncated to 4000 characters): {ing_trunc}\n"
        f"User skin scores: {json.dumps(skin)}\n"
        "Provide the JSON answer as described."
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def parse(text: str) -> Optional[Dict[str, Any]]:
    """Extract the first JSON object containing a ``verdict`` key from *text*.

    Normalises the verdict to lower‑case and validates it against the allowed
    set {"good", "care", "poor"}. Returns ``None`` on any parsing or validation
    failure.
    """
    decoder = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:
        try:
            obj, _ = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos = text.find("{", pos + 1); continue
        if isinstance(obj, dict):
            key = next((k for k in obj if isinstance(k, str) and k.lower() == "verdict"), None)
            if key is not None:
                v = obj.pop(key)
                v = v.strip().lower() if isinstance(v, str) else None
                if v not in {"good", "care", "poor"}:
                    return None
                obj["verdict"] = v
                return obj
        pos = text.find("{", pos + 1)
    return None


_ALIASES = {"pores": "pore", "wrinkles": "wrinkle", "dark_circle": "dark_circle_v2", "dark_circles": "dark_circle_v2",
            "dark circles": "dark_circle_v2", "oily": "oiliness", "oil": "oiliness", "hydration": "moisture", "dryness": "moisture"}


def _norm_concern(name: Any) -> Optional[str]:
    """Map a judge's concern label onto the YouCam concern keys (pores -> pore, etc.)."""
    if not isinstance(name, str) or not name.strip():
        return None
    n = name.strip().lower().replace("-", "_")
    return _ALIASES.get(n, _ALIASES.get(n.replace("_", " "), n))


def reconcile(results: Dict[str, Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """Combine the parsed answers of multiple judges into a single consensus.

    Parameters
    ----------
    results: mapping of judge name → parsed JSON (or ``None`` if parsing failed).

    Returns a dictionary with keys ``verdict``, ``agreement``, ``judges``,
    ``per_concern`` and ``dissent`` as described in the specification.

    Raises
    ------
    ValueError
        If *results* contains no valid (non‑``None``) judges.
    """
    # Filter out invalid judges.
    valid: Dict[str, Dict[str, Any]] = {
        judge: parsed for judge, parsed in results.items() if parsed is not None
    }

    if not valid:
        raise ValueError("No valid judges to reconcile.")

    # Determine majority verdict.
    verdict_counts: Dict[str, int] = {}
    for parsed in valid.values():
        v = parsed.get("verdict")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1

    # Find verdict(s) with highest count.
    max_count = max(verdict_counts.values())
    top_verdicts = [v for v, cnt in verdict_counts.items() if cnt == max_count]

    # Tie resolution: default to "care".
    majority_verdict = top_verdicts[0] if len(top_verdicts) == 1 else "care"

    # Agreement fraction.
    agreeing = sum(1 for parsed in valid.values() if parsed.get("verdict") == majority_verdict)
    agreement = agreeing / len(valid)

    # Build per_concern mapping.
    per_concern: Dict[str, Dict[str, Any]] = {}
    for judge, parsed in valid.items():
        for concern in parsed.get("concerns", []):
            name = _norm_concern(concern.get("concern"))
            effect = (concern.get("effect") or "").strip().lower() or None
            if name is None or effect is None:
                continue
            if name not in per_concern:
                per_concern[name] = {"effects": {}, "split": False}
            per_concern[name]["effects"][judge] = effect

    # Determine split flag for each concern.
    for data in per_concern.values():
        effects_set = set(data["effects"].values())
        data["split"] = len(effects_set) > 1

    # Identify dissenting judges.
    dissent = [
        judge for judge, parsed in valid.items() if parsed.get("verdict") != majority_verdict
    ]

    return {
        "verdict": majority_verdict,
        "agreement": agreement,
        "judges": len(valid),
        "per_concern": per_concern,
        "dissent": dissent,
    }
