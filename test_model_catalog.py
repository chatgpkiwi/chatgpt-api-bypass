"""Focused tests for the Phase 3 custom model-catalog generator."""

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("update_model_catalog.py")
SPEC = importlib.util.spec_from_file_location("update_model_catalog", MODULE_PATH)
assert SPEC and SPEC.loader
catalog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = catalog
SPEC.loader.exec_module(catalog)


def source_record():
    record = {key: None for key in catalog.EXPECTED_MODEL_KEYS}
    record.update({
        "slug": catalog.DEFAULT_MODEL, "apply_patch_tool_type": "freeform",
        "tool_mode": "code_mode_only", "multi_agent_version": "v1",
        "experimental_supported_tools": [], "supported_reasoning_levels": [],
        "shell_type": "shell_command", "visibility": "list", "supported_in_api": True,
        "priority": 1, "additional_speed_tiers": [], "service_tiers": [],
        "base_instructions": "preserve me", "include_skills_usage_instructions": False,
        "supports_reasoning_summaries": True, "default_reasoning_summary": "none",
        "support_verbosity": True, "web_search_tool_type": "text_and_image",
        "truncation_policy": {"mode": "tokens", "limit": 1000},
        "supports_parallel_tool_calls": True, "supports_image_detail_original": True,
        "effective_context_window_percent": 95, "input_modalities": ["text"],
        "supports_search_tool": True, "use_responses_lite": True,
    })
    return record


class ModelCatalogTests(unittest.TestCase):
    def test_generation_preserves_every_non_tool_field(self):
        source = source_record()
        generated = catalog.build_text_only_catalog(json.dumps({"models": [source]}), catalog.DEFAULT_MODEL)
        model = generated["models"][0]
        self.assertEqual(model["apply_patch_tool_type"], None)
        self.assertEqual(model["tool_mode"], "direct")
        self.assertEqual(model["multi_agent_version"], None)
        self.assertEqual(model["experimental_supported_tools"], [])
        for key, value in source.items():
            if key not in catalog.TEXT_ONLY_OVERRIDES:
                self.assertEqual(model[key], value)

    def test_schema_change_is_rejected(self):
        changed = source_record()
        changed["future_field"] = True
        with self.assertRaisesRegex(catalog.CatalogValidationError, "schema changed"):
            catalog.build_text_only_catalog(json.dumps({"models": [changed]}), catalog.DEFAULT_MODEL)

    def test_unreviewed_source_tool_metadata_is_rejected(self):
        changed = source_record()
        changed["apply_patch_tool_type"] = "function"
        with self.assertRaisesRegex(catalog.CatalogValidationError, "reviewed"):
            catalog.build_text_only_catalog(json.dumps({"models": [changed]}), catalog.DEFAULT_MODEL)

    def test_check_rejects_catalog_that_reintroduces_a_tool(self):
        generated = catalog.build_text_only_catalog(json.dumps({"models": [source_record()]}), catalog.DEFAULT_MODEL)
        broken = copy.deepcopy(generated)
        broken["models"][0]["apply_patch_tool_type"] = "freeform"
        with self.assertRaisesRegex(catalog.CatalogValidationError, "expected None"):
            catalog.validate_text_only_catalog(broken, catalog.DEFAULT_MODEL)

    def test_cli_failure_is_reported(self):
        completed = mock.Mock(returncode=1, stdout="", stderr="no catalog")
        with mock.patch.object(catalog.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(catalog.CatalogValidationError, "debug models.*no catalog"):
                catalog.raw_catalog_from_codex("codex")


if __name__ == "__main__":
    unittest.main()
