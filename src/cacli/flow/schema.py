"""Provider-independent structured-output enforcement helpers."""

from __future__ import annotations

import json
from typing import Any

import jsonschema


SCHEMA_TIERS = {
    "claude": "native",
    "codex": "native-file",
    "devin": "prompt",
    "cursor": "prompt",
}

SCHEMA_PROMPT = (
    "Respond with ONLY a JSON object matching this JSON Schema "
    "(no prose, no markdown code fences):\n{schema}"
)


def schema_tier(provider_name: str) -> str:
    """Return the structured-output strategy for a provider."""
    return SCHEMA_TIERS.get(provider_name, "prompt")


def canonical_schema(schema: dict[str, Any]) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


def strictify(schema: Any) -> Any:
    """Deep-copy a schema into OpenAI strict-mode form for codex --output-schema.

    Strict mode rejects any object node lacking ``additionalProperties: false``
    or whose ``required`` omits a defined property. Tightening only the
    generation side is safe: output that satisfies the strict variant always
    satisfies the user's original schema, which host-side validation still
    uses.
    """
    if isinstance(schema, list):
        return [strictify(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    output = {key: strictify(value) for key, value in schema.items()}
    if output.get("type") == "object" or "properties" in output:
        output.setdefault("additionalProperties", False)
        properties = output.get("properties")
        if isinstance(properties, dict):
            output["required"] = sorted(properties)
    return output


def decorate_prompt(prompt: str, schema: dict[str, Any]) -> str:
    return f"{prompt}\n\n{SCHEMA_PROMPT.format(schema=canonical_schema(schema))}"


def retry_prompt(decorated_prompt: str, error: str, previous_response: str) -> str:
    return (
        f"{decorated_prompt}\n\n"
        f"Your previous response failed validation: {error[:500]}\n"
        f"Previous response (truncated): {previous_response[:1000]}\n"
        "Return only corrected JSON."
    )


def extract_json(text: str) -> Any | None:
    """Extract the first balanced, decodable JSON object or array from text."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            stripped = "\n".join(lines[1:-1]).strip()

    for start, char in enumerate(stripped):
        if char not in "{[":
            continue
        stack: list[str] = []
        in_string = False
        escaped = False
        for end in range(start, len(stripped)):
            current = stripped[end]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current in "{[":
                stack.append("}" if current == "{" else "]")
            elif current in "}]":
                if not stack or current != stack[-1]:
                    break
                stack.pop()
                if not stack:
                    try:
                        value = json.loads(stripped[start : end + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(value, (dict, list)):
                        return value
                    break
    return None


def validate_output(candidate: Any, schema: dict[str, Any]) -> str | None:
    """Return the first validation error, or None when candidate conforms."""
    try:
        jsonschema.validate(candidate, schema)
    except (jsonschema.ValidationError, jsonschema.SchemaError) as exc:
        return exc.message
    return None
