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

from raw_request_store import (
    append_raw_error,
    append_raw_request,
    append_raw_response,
    cleanup_raw_request_logs,
)


class RawRequestStoreTest(unittest.TestCase):
    def test_append_raw_request_writes_complete_request_as_jsonl(self):
        recorded_at = datetime(2026, 8, 22, 12, 30, tzinfo=timezone.utc)
        request_body = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "结构化记忆 patch"}],
            "stream": False,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = append_raw_request(
                directory=Path(temp_dir),
                request_id="request-1",
                request_type="memory_maintenance",
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
                    "kind": "request",
                    "request_id": "request-1",
                    "request_type": "memory_maintenance",
                    "metadata": {},
                    "request": request_body,
                }
            ],
        )

    def test_append_raw_request_omits_image_base64_payload(self):
        request_body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,aGVsbG8="},
                        }
                    ],
                }
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = append_raw_request(
                directory=Path(temp_dir),
                request_id="request-image",
                request_type="chat_reply",
                request_body=request_body,
            )
            record = json.loads(log_path.read_text(encoding="utf-8"))

        logged_url = record["request"]["messages"][0]["content"][0]["image_url"]["url"]
        self.assertEqual(
            logged_url,
            {
                "$image_base64_omitted": {
                    "mime_type": "image/png",
                    "encoded_chars": 8,
                    "encoded_sha256": (
                        "333d6b3a3c1f5db6c9bdda5939b13698"
                        "6d170f4649172a68368d54ecb44c2ff2"
                    ),
                }
            },
        )
        self.assertNotIn("aGVsbG8=", json.dumps(record))

    def test_append_response_and_error_share_request_correlation_fields(self):
        recorded_at = datetime(2026, 8, 22, 12, 30, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            log_path = append_raw_response(
                directory=directory,
                request_id="request-1",
                request_type="direct_chat_agent",
                response_body={"choices": [{"message": {"content": "原始回复"}}]},
                metadata={"provider_request_id": "upstream-1", "status_code": 200},
                handling={"upstream_blocked": False, "outbound_content": "原始回复"},
                now=recorded_at,
            )
            append_raw_error(
                directory=directory,
                request_id="request-2",
                request_type="direct_chat_agent",
                error={"type": "BadRequestError", "body": {"code": "blocked"}},
                metadata={"provider_request_id": "upstream-2", "status_code": 400},
                now=recorded_at,
            )
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(records[0]["kind"], "response")
        self.assertEqual(records[0]["request_id"], "request-1")
        self.assertEqual(
            records[0]["response"]["choices"][0]["message"]["content"],
            "原始回复",
        )
        self.assertEqual(records[0]["handling"]["outbound_content"], "原始回复")
        self.assertEqual(records[1]["kind"], "error")
        self.assertEqual(records[1]["request_id"], "request-2")
        self.assertEqual(records[1]["error"]["body"]["code"], "blocked")

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
