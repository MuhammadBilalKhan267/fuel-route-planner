import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.coordinates.loves_ta import write_json


class AtomicJsonWriterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "journal.json"
        self.path.write_text('{"old": true}', encoding="utf-8")
        self.temporary = self.path.with_suffix(".json.tmp")

    def test_short_sharing_violation_retries_replace_without_rewriting_content(self):
        replace = Path.replace
        attempts = []

        def transient_replace(temporary, destination):
            attempts.append((temporary, destination))
            if len(attempts) < 3:
                self.assertEqual(json.loads(self.path.read_text()), {"old": True})
                raise PermissionError("Temporary sharing violation")
            return replace(temporary, destination)

        with (
            patch.object(Path, "replace", transient_replace),
            patch("scripts.coordinates.loves_ta.time.sleep") as sleep,
        ):
            write_json(self.path, {"new": "value"})

        self.assertEqual(attempts, [(self.temporary, self.path)] * 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.05, 0.1])
        self.assertEqual(json.loads(self.path.read_text()), {"new": "value"})
        self.assertFalse(self.temporary.exists())

    def test_exhausted_sharing_violations_preserve_old_destination_and_raise(self):
        with (
            patch.object(Path, "replace", side_effect=PermissionError("Locked")) as replace,
            patch("scripts.coordinates.loves_ta.time.sleep") as sleep,
            self.assertRaises(PermissionError),
        ):
            write_json(self.path, {"new": True})

        self.assertEqual(replace.call_count, 5)
        self.assertEqual(sleep.call_count, 4)
        self.assertEqual(json.loads(self.path.read_text()), {"old": True})
        self.assertEqual(json.loads(self.temporary.read_text()), {"new": True})

    def test_unrelated_replace_error_is_not_retried(self):
        with (
            patch.object(Path, "replace", side_effect=FileNotFoundError("Missing")) as replace,
            patch("scripts.coordinates.loves_ta.time.sleep") as sleep,
            self.assertRaises(FileNotFoundError),
        ):
            write_json(self.path, {"new": True})

        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), {"old": True})

    def test_permission_failure_while_writing_temporary_file_is_not_retried(self):
        with (
            patch.object(Path, "write_text", side_effect=PermissionError("Read only")) as write,
            patch.object(Path, "replace") as replace,
            patch("scripts.coordinates.loves_ta.time.sleep") as sleep,
            self.assertRaises(PermissionError),
        ):
            write_json(self.path, {"new": True})

        self.assertEqual(write.call_count, 1)
        replace.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), {"old": True})


if __name__ == "__main__":
    unittest.main()
