# -*- coding: utf-8 -*-
"""A saved startup must bypass inputs and preserve all trained artifacts."""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import product_portable as portable
from tools import product_saved_start as saved


class SavedStartTests(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory()
        self.root = Path(self.holder.name)
        self.output = self.root / "outputs" / "run_frozen"
        self.output.mkdir(parents=True)
        for name in ("state", "config"):
            (self.root / name).mkdir()
        (self.root / "config" / "portable.json").write_text(json.dumps({"ports": {"price": 1, "effectiveness": 2, "recommendation": 3}}), encoding="utf-8")
        files = {"price_model": "price.pkl", "effectiveness_model": "effect.json", "business_package": "business.json"}
        hashes = {}
        for key, name in files.items():
            raw = ("unchanged " + key).encode("utf-8")
            (self.output / name).write_bytes(raw)
            hashes[key] = hashlib.sha256(raw).hexdigest()
        self.manifest = dict(files, artifact_sha256=hashes, status="ready")
        (self.output / "prototype_manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        (self.root / "state" / "latest.json").write_text(json.dumps({"output": "outputs/run_frozen", "fingerprint": "old configuration"}), encoding="utf-8")
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(portable, "ROOT", self.root))
        self.stack.enter_context(patch.object(portable, "verify_runtime", return_value={"status": "PASS"}))
        self.prepare = self.stack.enter_context(patch.object(portable, "prepare", side_effect=AssertionError("input read")))
        self.resolve = self.stack.enter_context(patch.object(portable, "resolve_input", side_effect=AssertionError("input resolution")))
        self.rebuild = self.stack.enter_context(patch.object(portable, "rebuild", side_effect=AssertionError("training")))
        self.serve = self.stack.enter_context(patch("tools.product_pipeline.serve"))

    def tearDown(self):
        self.stack.close()
        self.holder.cleanup()

    def run_saved(self, args=None):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return saved.main(args or ["--no-browser"])

    def test_missing_input_starts_saved_model_without_training(self):
        self.assertFalse((self.root / "input").exists())
        self.assertEqual(self.run_saved(), 0)
        self.assertEqual(self.serve.call_args[0][0], self.output)
        self.prepare.assert_not_called()
        self.resolve.assert_not_called()
        self.rebuild.assert_not_called()

    def test_invalid_changed_table_is_not_read(self):
        (self.root / "input").mkdir()
        (self.root / "input" / "products.xlsx").write_bytes(b"broken workbook")
        self.assertEqual(self.run_saved(), 0)
        self.rebuild.assert_not_called()

    def test_repeated_start_preserves_state_and_model_hashes(self):
        state = (self.root / "state" / "latest.json").read_bytes()
        for unused in range(2):
            self.assertEqual(self.run_saved(), 0)
        self.assertEqual((self.root / "state" / "latest.json").read_bytes(), state)
        for key in ("price_model", "effectiveness_model", "business_package"):
            self.assertEqual(portable.digest(self.output / self.manifest[key]), self.manifest["artifact_sha256"][key])

    def test_corrupt_model_does_not_trigger_retraining(self):
        (self.output / "price.pkl").write_bytes(b"changed")
        self.assertEqual(self.run_saved(), 1)
        self.serve.assert_not_called()
        self.rebuild.assert_not_called()

    def test_missing_saved_model_requires_explicit_training(self):
        (self.root / "state" / "latest.json").unlink()
        self.assertEqual(self.run_saved(), 1)
        self.rebuild.assert_not_called()

    def test_corrupt_state_does_not_trigger_retraining(self):
        (self.root / "state" / "latest.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.run_saved(), 1)
        self.rebuild.assert_not_called()

    def test_runtime_ports_are_forwarded(self):
        self.assertEqual(self.run_saved(["--no-browser", "--price-port", "0", "--effect-port", "0", "--app-port", "0"]), 0)
        self.assertEqual(self.serve.call_args[1]["app_port"], 0)
        self.rebuild.assert_not_called()

    def test_fixed_version_paths_have_no_escape_control_bytes(self):
        command = saved.fixed_version_launcher_bytes().split(b"\r\n")[3]
        self.assertIn(b'..\\..\\runtime\\python38\\python.exe', command)
        self.assertIn(b'..\\..\\tools\\product_portable.py', command)
        self.assertNotIn(b"\r", command)
        self.assertNotIn(b"\t", command)
        self.assertIn(b"start --output", command)
    def test_r4_start_uses_saved_policy_without_input(self):
        settings=json.loads((self.root/"config"/"portable.json").read_text(encoding="utf-8"))
        settings["startup_policy"]="saved_only"
        (self.root/"config"/"portable.json").write_text(json.dumps(settings),encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(portable.main(["start","--no-browser"]),0)
        self.prepare.assert_not_called(); self.resolve.assert_not_called(); self.rebuild.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
