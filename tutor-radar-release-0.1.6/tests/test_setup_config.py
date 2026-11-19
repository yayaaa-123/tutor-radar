from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from tutor_monitor.setup_config import save_profile
from tutor_monitor.service import apply_personal_conditions, load_config


class SetupConfigTests(unittest.TestCase):
    def test_profile_starts_without_a_placeholder_group(self):
        with patch("tutor_monitor.setup_config.atomic_write_text") as write:
            save_profile(Path("config.toml"), {
                "gender": "female", "education": "硕士", "subjects": ["英语"],
                "school": '示例"学校', "origin": "深圳大剧院", "weekend_only": False,
            })
        data = tomllib.loads(write.call_args.args[1])
        self.assertEqual(data["wechat"]["groups"], [])
        self.assertEqual(data["profile"]["subjects"], ["英语"])
        self.assertEqual(data["scoring"]["capability"]["experience"], [])
        self.assertFalse(data["profile"]["weekend_only"])

    def test_edit_applies_without_rewriting_base_config(self):
        path = Path(__file__).resolve().parents[1] / "config.example.toml"
        payload = {
            "gender": "female", "education": "硕士", "subjects": ["英语"],
            "origin": "深圳大剧院", "weekend_only": False,
        }
        with patch("tutor_monitor.setup_config.atomic_write_json") as write:
            save_profile(path, payload)
        target, saved = write.call_args.args
        self.assertEqual(target.name, "profile.local.json")
        self.assertNotIn("db_dir", saved)
        config = load_config(path)
        with patch("tutor_monitor.service.read_profile_override", return_value=saved):
            changed = apply_personal_conditions(config)
        self.assertEqual(changed.groups, config.groups)
        self.assertEqual(changed.model_settings, config.model_settings)
        self.assertEqual(changed.map_settings.key_file, config.map_settings.key_file)
        self.assertEqual(changed.map_settings.origin_address, "深圳大剧院")
        self.assertEqual(changed.profile.subjects, ("英语",))
        self.assertEqual(changed.capability_profile.strength_subjects, ("英语",))
