import unittest
from pathlib import Path
from unittest.mock import patch

from tutor_monitor import model_setup
from tutor_monitor import service
from threading import Event


class ModelSetupTests(unittest.TestCase):
    directory = Path("D:/test/models")

    def setUp(self):
        self.original_status = model_setup.model_status()
        self.addCleanup(model_setup._set, self.original_status["status"], self.original_status["percent"])
        for name, value in (
            ("find_ollama", "ollama.exe"), ("start", None), ("owns_listener", True),
        ):
            mock = patch.object(model_setup.ollama_runtime, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        for target in ("tutor_monitor.model_setup.NamedTemporaryFile", "pathlib.Path.mkdir"):
            mock = patch(target)
            mock.start()
            self.addCleanup(mock.stop)

    def test_missing_model_check_does_not_download(self):
        with patch.object(model_setup.requests, "Session") as session_type:
            session = session_type.return_value.__enter__.return_value
            session.get.return_value.json.return_value = {"models": []}
            model_setup._prepare(self.directory)
            session.post.assert_not_called()
            self.assertEqual(model_setup.model_status()["status"], "download_required")

    def test_present_model_is_reused(self):
        with patch.object(model_setup.requests, "Session") as session_type, patch.object(model_setup, "atomic_write_json"):
            session = session_type.return_value.__enter__.return_value
            session.get.return_value.json.return_value = {"models": [{"name": model_setup.MODEL_NAME}]}
            model_setup._prepare(self.directory)
            session.post.assert_not_called()
            self.assertEqual(model_setup.model_status()["status"], "ready")

    def test_download_requires_prior_detection(self):
        model_setup._set("idle")
        with patch.object(model_setup, "Thread") as worker:
            self.assertFalse(model_setup.start_model_setup(str(self.directory), download=True))
            worker.assert_not_called()
            with patch.dict(model_setup._status, status="download_required", directory=str(self.directory)):
                self.assertFalse(model_setup.start_model_setup("D:/different-models", download=True))
                self.assertTrue(model_setup.start_model_setup(str(self.directory), download=True))
                worker.assert_called_once_with(target=model_setup._prepare, args=(self.directory, True), daemon=True)

    def test_unowned_listener_blocks_confirmed_download(self):
        with patch.object(model_setup.requests, "Session") as session_type, patch.object(model_setup.ollama_runtime, "owns_listener", return_value=False):
            session = session_type.return_value.__enter__.return_value
            session.get.return_value.json.return_value = {"models": []}
            model_setup._prepare(self.directory, download=True)
            session.post.assert_not_called()
            self.assertEqual(model_setup.model_status()["status"], "error")

    def test_running_monitor_enables_model_once_after_setup(self):
        monitor = service.TutorMonitorService.__new__(service.TutorMonitorService)
        monitor.config = service.load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        monitor.model_pipeline = None
        monitor._repair_wake = Event()
        with patch.object(service, "configured_directory", return_value="") as directory, patch.object(service, "ModelPipeline") as pipeline:
            monitor._enable_ready_model()
            pipeline.assert_not_called()
            directory.return_value = str(self.directory)
            monitor._enable_ready_model()
            monitor._enable_ready_model()
            pipeline.assert_called_once()
            pipeline.return_value.start.assert_called_once()
            self.assertEqual(monitor.config.model_settings.endpoint, service.MANAGED_OLLAMA_ENDPOINT)
            self.assertTrue(monitor._repair_wake.is_set())
