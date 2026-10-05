from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from astrbot_plugin_memos_memory.thread_policy import POLICY_VERSION, PRESETS


ROOT = Path(sys.modules["astrbot_plugin_memos_memory.thread_policy"].__file__).resolve().parent
MODEL_DIR = ROOT / "assets" / "live2d" / "seethrough_output_1"


class Test11ReleaseTests(unittest.TestCase):
    def test_live2d_export_contains_new_motion_parameters_and_physics(self):
        display = json.loads((MODEL_DIR / "seethrough_output_1.cdi3.json").read_text(encoding="utf-8"))
        parameter_ids = {item["Id"] for item in display["Parameters"]}
        new_ids = {
            "ParamArmR", "ParamArmL", "ParamSleeveR", "ParamSleeveL",
            "ParamSkirtSway", "ParamTorsoBreath", "ParamWristR", "ParamWristL",
            "ParamShoulderR", "ParamShoulderL", "ParamTorsoLean", "ParamHemFlutter",
        }
        self.assertTrue(new_ids.issubset(parameter_ids))

        physics = json.loads((MODEL_DIR / "seethrough_output_1.physics3.json").read_text(encoding="utf-8"))
        outputs = {
            output["Destination"]["Id"]
            for group in physics["PhysicsSettings"]
            for output in group.get("Output", [])
        }
        self.assertTrue({"ParamSleeveR", "ParamSleeveL", "ParamSkirtSway", "ParamTorsoBreath"}.issubset(outputs))

    def test_live2d_browser_driver_is_parameter_level_and_observable(self):
        source = (ROOT / "assets" / "house-live2d.js").read_text(encoding="utf-8")
        for parameter_id in (
            "ParamArmR", "ParamArmL", "ParamWristR", "ParamWristL",
            "ParamShoulderR", "ParamShoulderL", "ParamTorsoLean", "ParamHemFlutter",
        ):
            self.assertIn(parameter_id, source)
        self.assertIn("applyMicroMotion", source)
        self.assertIn("micro_motion", source)
        self.assertIn("Math.sin(Math.PI * progress)", source)
        for parameter_id in (
            "ParamAngleX", "ParamAngleY", "ParamAngleZ", "ParamEyeBallX", "ParamEyeBallY",
        ):
            self.assertIn(parameter_id, source)
        self.assertIn("applyHeadControl", source)
        self.assertIn("pointer_tracking", source)
        self.assertIn("POINTER_LIMITS", source)
        self.assertIn("softPointerAxis", source)
        self.assertIn("pointer_limits", source)
        self.assertIn("const tracking = !action", source)
        self.assertNotIn("px * 3.4", source)
        self.assertNotIn("naturalZ - px", source)
        self.assertNotIn("this.model.focus(", source)
        self.assertNotIn("this.model.rotation", source)
        self.assertNotIn("this.model.scale.set(scale *", source)

    def test_editable_live2d_sources_are_not_in_runtime_tree(self):
        names = {path.name.lower() for path in MODEL_DIR.rglob("*") if path.is_file()}
        self.assertFalse(any(name.endswith(".cmo3") for name in names))
        self.assertFalse(any(name.endswith(".psd2live.json") for name in names))

    def test_presets_are_controlled_and_never_modify_sensitive_configuration(self):
        self.assertEqual(POLICY_VERSION, "rc1-policy-v1")
        self.assertEqual(PRESETS["balanced"]["thread_mode"], "canary")
        self.assertEqual(PRESETS["balanced"]["thread_canary_percent"], 10)
        self.assertEqual(PRESETS["effect"]["thread_canary_percent"], 20)
        self.assertEqual(PRESETS["conservative"]["thread_mode"], "shadow")
        self.assertEqual(PRESETS["cost"]["thread_mode"], "shadow")
        self.assertTrue(all(values["thread_memory_enable"] for values in PRESETS.values()))
        self.assertTrue(all(values["thread_worker_enable"] for values in PRESETS.values()))
        protected_fragments = (
            "url", "token", "provider_id", "embedding", "rerank", "character",
            "role", "path", "port", "seed", "allowlist", "denylist", "lock",
        )
        for preset_name, values in PRESETS.items():
            with self.subTest(preset=preset_name):
                for key, value in values.items():
                    self.assertIsInstance(value, (str, int, float, bool))
                    self.assertFalse(any(fragment in key for fragment in protected_fragments), key)

    def test_current_release_identity_is_consistent(self):
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("version: 6.1.0", metadata)
        self.assertIn("当前版本：`6.1.0`", readme)


if __name__ == "__main__":
    unittest.main()
