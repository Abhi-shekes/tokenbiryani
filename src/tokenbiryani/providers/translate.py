"""The one place the gateway rewrites a request body, and why.

Elsewhere the rule is absolute: route, never rewrite. Bedrock and Vertex serve the
same Messages API but address the model in the URL rather than the body, and each
stamps its own `anthropic_version`. So the translation is exactly two edits, applied
nowhere else, and everything the caller sent otherwise travels untouched.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

BEDROCK_VERSION = "bedrock-2023-05-31"
VERTEX_VERSION = "vertex-2023-10-16"


def to_platform_body(body: Mapping[str, Any], anthropic_version: str) -> Dict[str, Any]:
    """Drop `model` (it moves into the URL) and stamp the platform's version."""
    translated = {key: value for key, value in body.items() if key != "model"}
    translated["anthropic_version"] = anthropic_version
    return translated


def map_model(model: str, model_map: Mapping[str, str]) -> str:
    """Resolve a caller's model name to the platform's id.

    An exact entry wins; then a trailing-wildcard prefix; then the name as given,
    which is right whenever the caller already knows the platform's id.
    """
    if model in model_map:
        return model_map[model]
    for pattern, target in model_map.items():
        if pattern.endswith("*") and model.startswith(pattern[:-1]):
            return target
    return model
