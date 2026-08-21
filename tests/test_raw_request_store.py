import json
import stat
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


PLUGIN_DIR = (
    Path(__file__).resolve().parents[1] / "my-bot" / "plugins" / "multi_llm_chat"
)
sys.path.insert(0, str(PLUGIN_DIR))

from raw_request_store import append_raw_request, cleanup_raw_request_logs


class RawRequestStoreTest(unittest.TestCase):
    def test_append_raw_request_writes_complete_request_as_jsonl(self):
        recorded_at = datetime(2026, 8, 22, 12, 30, tzinfo=timezone.utc)
        request_body = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "群聊记忆 Markdown"}],
            "stream": False,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = append_raw_request(
                directory=Path(temp_dir),
                request_id="request-1",
                request_type="memory_update",
                request_body=request_body,
                now=recorded_at,
            )

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            log_file_mode = stat.S_IMODE(log_path.stat().st_mode)

        self.assertEqual(log_path.name, "llm-requests-2026-08-22.jsonl")
        self.assertEqual(log_file_mode, 0o600)
        self.assertEqual(
            records,
            [
                {
                    "recorded_at": "2026-08-22T12:30:00+00:00",
                    "request_id": "request-1",
                    "request_type": "memory_update",
                    "request": request_body,
                }
            ],
        )

    def test_cleanup_deletes_only_logs_older_than_retention_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            expired_log = directory / "llm-requests-2026-08-15.jsonl"
            boundary_log = directory / "llm-requests-2026-08-16.jsonl"
            current_log = directory / "llm-requests-2026-08-22.jsonl"
            invalid_log = directory / "llm-requests-invalid.jsonl"
            unrelated_file = directory / "notes.jsonl"
            for path in [
                expired_log,
                boundary_log,
                current_log,
                invalid_log,
                unrelated_file,
            ]:
                path.write_text("{}\n", encoding="utf-8")

            deleted_count = cleanup_raw_request_logs(
                directory=directory,
                retention_days=7,
                today=date(2026, 8, 22),
            )

            self.assertEqual(deleted_count, 1)
            self.assertFalse(expired_log.exists())
            self.assertTrue(boundary_log.exists())
            self.assertTrue(current_log.exists())
            self.assertTrue(invalid_log.exists())
            self.assertTrue(unrelated_file.exists())


if __name__ == "__main__":
    unittest.main()
