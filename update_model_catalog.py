#!/usr/bin/env python3
"""Build the text-only Codex catalog used only by the proxy profile.

The catalog schema is deliberately pinned to the installed Codex CLI 0.144.6
record shape. A future schema change must be reviewed instead of being copied
silently into the proxy catalog.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_ROOT / "codex-api-model-catalog.json"
DEFAULT_MODEL = "gpt-5.6-luna"
EXPECTED_ROOT_KEYS = {"models"}
# Serialized ModelInfo fields in Codex CLI 0.144.6. Reject schema drift.
EXPECTED_MODEL_KEYS = {
    "additional_speed_tiers", "apply_patch_tool_type", "availability_nux",
    "base_instructions", "comp_hash", "context_window", "default_reasoning_level",
    "default_reasoning_summary", "default_verbosity", "description", "display_name",
    "effective_context_window_percent", "experimental_supported_tools",
    "include_skills_usage_instructions", "input_modalities", "max_context_window",
    "model_messages", "multi_agent_version", "priority", "service_tiers", "shell_type",
    "slug", "support_verbosity", "supported_in_api", "supported_reasoning_levels",
    "supports_image_detail_original", "supports_parallel_tool_calls",
    "supports_reasoning_summaries", "supports_search_tool", "tool_mode",
    "truncation_policy", "upgrade", "use_responses_lite", "visibility",
    "web_search_tool_type",
}
TEXT_ONLY_OVERRIDES = {
    "apply_patch_tool_type": None,
    "tool_mode": "direct",
    "multi_agent_version": None,
    "experimental_supported_tools": [],
}


class CatalogValidationError(ValueError):
    """The installed catalog no longer matches the reviewed schema."""


def parse_catalog(raw_json: str, model_slug: str) -> dict[str, Any]:
    """Parse and strictly validate a raw `codex debug models` response."""
    try:
        catalog = json.loads(raw_json)
    except json.JSONDecodeError as error:
        raise CatalogValidationError(f"Codex did not return valid catalog JSON: {error}") from error
    if not isinstance(catalog, dict) or set(catalog) != EXPECTED_ROOT_KEYS:
        raise CatalogValidationError(
            "Codex catalog root schema changed; expected exactly {'models'}. "
            "Update this script after reviewing the new CLI schema."
        )
    models = catalog.get("models")
    if not isinstance(models, list):
        raise CatalogValidationError("Codex catalog `models` must be an array.")
    matches = [item for item in models if isinstance(item, dict) and item.get("slug") == model_slug]
    if len(matches) != 1:
        raise CatalogValidationError(
            f"Expected exactly one {model_slug!r} record in `codex debug models`; found {len(matches)}."
        )
    model = matches[0]
    if set(model) != EXPECTED_MODEL_KEYS:
        missing = sorted(EXPECTED_MODEL_KEYS - set(model))
        unexpected = sorted(set(model) - EXPECTED_MODEL_KEYS)
        raise CatalogValidationError(
            "Codex model schema changed; review and update this script before generating a catalog "
            f"(missing={missing}, unexpected={unexpected})."
        )
    if model["apply_patch_tool_type"] != "freeform":
        raise CatalogValidationError(
            "The source model no longer uses the reviewed `freeform` apply-patch tool; "
            "review this change before generating a catalog."
        )
    if model["tool_mode"] not in {"code_mode", "code_mode_only"}:
        raise CatalogValidationError(
            "The source model has an unreviewed tool_mode; review this change before generating a catalog."
        )
    if not isinstance(model["experimental_supported_tools"], list):
        raise CatalogValidationError("`experimental_supported_tools` must be an array.")
    return model


def build_text_only_catalog(raw_json: str, model_slug: str) -> dict[str, Any]:
    """Copy the selected live record and make only reviewed tool changes."""
    source_model = parse_catalog(raw_json, model_slug)
    text_only_model = copy.deepcopy(source_model)
    text_only_model.update(TEXT_ONLY_OVERRIDES)
    for key, value in source_model.items():
        if key not in TEXT_ONLY_OVERRIDES and text_only_model[key] != value:
            raise AssertionError(f"unexpected mutation to {key}")
    return {"models": [text_only_model]}


def validate_text_only_catalog(catalog: dict[str, Any], model_slug: str) -> None:
    """Validate the generated catalog without accepting unknown schema fields."""
    if not isinstance(catalog, dict) or set(catalog) != EXPECTED_ROOT_KEYS:
        raise CatalogValidationError("Generated catalog has an unexpected root schema.")
    models = catalog.get("models")
    if not isinstance(models, list) or len(models) != 1 or not isinstance(models[0], dict):
        raise CatalogValidationError("Generated catalog must contain exactly one model record.")
    model = models[0]
    if set(model) != EXPECTED_MODEL_KEYS or model.get("slug") != model_slug:
        raise CatalogValidationError("Generated catalog model schema or slug is invalid.")
    for key, expected in TEXT_ONLY_OVERRIDES.items():
        if model.get(key) != expected:
            raise CatalogValidationError(f"Generated catalog has {key}={model.get(key)!r}, expected {expected!r}.")


def raw_catalog_from_codex(codex_binary: str) -> str:
    result = subprocess.run([codex_binary, "debug", "models"], text=True, capture_output=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CatalogValidationError(f"`{codex_binary} debug models` failed: {detail[-1000:]}")
    return result.stdout


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as temporary:
            temporary_name = temporary.name
            json.dump(payload, temporary, indent=2)
            temporary.write("\n")
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="validate an existing generated catalog")
    args = parser.parse_args(argv)
    try:
        if args.check:
            validate_text_only_catalog(json.loads(args.output.read_text(encoding="utf-8")), args.model)
            print(f"Validated {args.output}")
            return 0
        generated = build_text_only_catalog(raw_catalog_from_codex(args.codex_binary), args.model)
        validate_text_only_catalog(generated, args.model)
        atomic_write_json(args.output, generated)
        print(f"Wrote {args.output}")
        return 0
    except (OSError, CatalogValidationError, json.JSONDecodeError) as error:
        print(f"Catalog update failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
