import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "paths.py"


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Phoenix"
        (self.root / "dashboard").mkdir(parents=True)
        shutil.copy2(SOURCE, self.root / "dashboard/paths.py")
        self.selection = self.root / ".runtime/dashboard-accounts.json"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, env=None):
        with patch.dict(os.environ, env or {}, clear=True):
            spec = importlib.util.spec_from_file_location(
                "isolated_paths", self.root / "dashboard/paths.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

    def fresh_env(self):
        runtime = self.root / ".runtime/sessions/fresh-test"
        return {
            "CASCADE_RUNTIME_DIR": str(runtime),
            "CASCADE_FINAL_STATE": str(runtime / "data/final.state.json"),
            "CASCADE_FINAL3_STATE": str(runtime / "data/final3.state.json"),
            "CASCADE_PHOENIX_STATE": str(runtime / "data/phoenix.state.json"),
        }

    def test_fresh_accounts_ignore_all_old_files_and_persist_after_restart(self):
        old = self.root.parent / "Final-3"
        (old / ".runtime").mkdir(parents=True)
        (old / ".runtime/account-location").write_text(
            str(self.root.parent / "old-runtime")
        )
        (old / "paper_state_final3.json").write_text('{"balance_usdt":12}')
        (self.root / "paper_state_phoenix.json").write_text('{"balance":9}')
        fresh = self.load(self.fresh_env())
        self.assertTrue(all(not p.exists() for p in fresh.STATE_PATHS.values()))
        self.assertEqual(len(set(fresh.STATE_PATHS.values())), 3)
        for p in fresh.STATE_PATHS.values():
            self.assertTrue(p.is_relative_to(fresh.RUNTIME))
        fresh.save_selection()
        restarted = self.load()
        self.assertEqual(restarted.RUNTIME, fresh.RUNTIME)
        self.assertEqual(restarted.STATE_PATHS, fresh.STATE_PATHS)
        self.assertEqual(
            json.loads((old / "paper_state_final3.json").read_text())["balance_usdt"],
            12,
        )
        self.assertEqual(
            json.loads((self.root / "paper_state_phoenix.json").read_text())["balance"],
            9,
        )

    def test_normal_restart_keeps_current_session_balances(self):
        fresh = self.load(self.fresh_env())
        fresh.save_selection()
        for path in fresh.STATE_PATHS.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"balance":105,"trades":[]}')
        restart = self.load({"CASCADE_RUNTIME_DIR": str(fresh.RUNTIME)})
        self.assertTrue(
            all(
                json.loads(p.read_text())["balance"] == 105
                for p in restart.STATE_PATHS.values()
            )
        )

    def test_different_runtime_does_not_import_saved_session_paths(self):
        self.load(self.fresh_env()).save_selection()
        another = self.root / ".runtime/other"
        module = self.load({"CASCADE_RUNTIME_DIR": str(another)})
        self.assertEqual(module.SAVED_STATES, {})
        self.assertEqual(module.STATE_PATHS["final"], another / "data/final.state.json")

    def test_duplicate_state_paths_are_rejected(self):
        env = self.fresh_env()
        env["CASCADE_FINAL3_STATE"] = env["CASCADE_FINAL_STATE"]
        with self.assertRaises(ValueError):
            self.load(env)

    def test_bad_selection_does_not_silently_resume_old_accounts(self):
        self.selection.parent.mkdir(parents=True)
        self.selection.write_text('{"version":999}')
        with self.assertRaises(ValueError):
            self.load()


if __name__ == "__main__":
    unittest.main()
