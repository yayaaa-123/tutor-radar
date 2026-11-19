import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tutor_monitor import dashboard_api as api
from tutor_monitor.maps import MapSettings


class MapBetaImportTests(unittest.TestCase):
    credentials = {"web_key": "a" * 32, "js_key": "b" * 32, "security_code": "c" * 32}

    def test_bundled_basemap_overrides_existing_local_js_config(self):
        bundled = {"js_key": "d" * 32, "security_code": "e" * 32, "style_id": "f" * 32}
        with (
            patch.dict("os.environ", TUTOR_RADAR_BASEMAP="bundled.json"),
            patch.object(api, "read_map_beta", return_value=self.credentials),
            patch.object(Path, "read_text", return_value=json.dumps(bundled)),
        ):
            config = api.build_map_client_config(Path("missing.toml"))
        self.assertTrue(config["configured"])
        for field, value in bundled.items():
            self.assertEqual(config[field], value)

    def test_malformed_credentials_do_not_reach_provider(self):
        with patch.object(api, "AmapCommuteEnricher") as provider:
            with self.assertRaises(ValueError):
                api.import_map_beta({**self.credentials, "web_key": "invalid"})
            provider.assert_not_called()

    def test_only_successful_transit_check_replaces_config(self):
        settings = MapSettings(False, Path("unused"), Path("unused"), "unused", "深圳市")
        with (
            patch("tutor_monitor.service.load_config", return_value=SimpleNamespace(map_settings=settings)),
            patch.object(api, "TemporaryDirectory"),
            patch.object(api, "AmapCommuteEnricher") as provider,
            patch.object(api, "save_map_beta") as save,
        ):
            client = provider.return_value
            client.enrich_between.return_value = {"status": "error"}
            self.assertFalse(api.import_map_beta(self.credentials)["ok"])
            save.assert_not_called()

            client.enrich_between.return_value = {"status": "ok", "route": {"duration_minutes": 20}}
            self.assertEqual(api.import_map_beta(self.credentials), {"ok": True, "transit_minutes": 20})
            save.assert_called_once_with(self.credentials)
