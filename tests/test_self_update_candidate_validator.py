from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "validate-self-update-candidate"
LOADER = importlib.machinery.SourceFileLoader("self_update_candidate_validator", str(VALIDATOR_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
validator = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(validator)


class SelfUpdateCandidateValidatorTests(unittest.TestCase):
    def test_candidate_runner_can_migrate_and_open_private_database_copy(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            db_path = Path(temporary) / "runner.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE legacy(value TEXT NOT NULL)")
            connection.commit()
            connection.close()
            os.chmod(db_path, 0o600)
            with mock.patch.dict(os.environ, {"MAC_RUNNER_SELF_UPDATE_DB_COPY": str(db_path)}):
                self.assertTrue(validator.validate_migration_copy(ROOT))
            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertIsNotNone(
                    connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
