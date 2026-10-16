"""Offline checks using a fake mailbox; no credentials or network required."""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collect_email_headers import FIELDS, choose_output, collect_headers, folder_display_name, save_csv, spreadsheet_text


class FakeMailbox:
    def __init__(self):
        self.folder = None
        self.fetches = []

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\Noselect) "/" "Container"',
                      b'(\\HasNoChildren) "/" "Archive"']

    def select(self, folder, readonly=False):
        assert readonly is True
        self.folder = folder
        return "OK", [b"503"]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return name, [b"99"]

    def uid(self, command, *args):
        if command == "search":
            count = 503 if self.folder == b'"INBOX"' else 1
            return "OK", [b" ".join(str(i).encode() for i in range(1, count + 1))]
        assert command == "fetch"
        assert "BODY.PEEK[HEADER.FIELDS" in args[1]
        self.fetches.append(args[0])
        return "OK", [
            (b"1 (UID " + uid + b" BODY[HEADER] {100}",
             b"Subject: =?utf-8?b?5rWL6K+V?=\r\nFrom: Teacher <teacher@example.com>\r\n"
             b"Message-ID: <same-id>\r\n\r\n")
            for uid in args[0].split(b",")
        ]


class CollectionTests(unittest.TestCase):
    def test_all_messages_all_selectable_folders(self):
        mailbox = FakeMailbox()
        rows = collect_headers(mailbox)
        self.assertEqual(len(rows), 504)
        self.assertEqual(len(mailbox.fetches), 7)
        self.assertEqual(len({row["id"] for row in rows}), 504)
        self.assertEqual({row["folder"] for row in rows}, {"INBOX", "Archive"})
        self.assertTrue(all(row["label"] == "" for row in rows))
        self.assertEqual(rows[0]["subject"], "测试")
        self.assertEqual(rows[0]["sender"], "Teacher <teacher@example.com>")

    def test_optional_limit(self):
        self.assertEqual(len(collect_headers(FakeMailbox(), limit=10)), 10)

    def test_folder_decode_and_formula_handling(self):
        self.assertEqual(folder_display_name(b'"&ZeVnLIqe-"'), "日本語")
        self.assertEqual(folder_display_name(b'"Research &- Work"'), "Research & Work")
        self.assertEqual(spreadsheet_text("=1+1"), "'=1+1")
        self.assertEqual(spreadsheet_text(""), "")

    def test_append_preserves_labels_extra_columns_and_skips_duplicates(self):
        with tempfile.TemporaryDirectory(dir=".") as directory:
            output = Path(directory) / "existing.csv"
            old = dict.fromkeys(FIELDS + ["category"], "")
            old.update(id="old", subject="课程通知", label="0", message_id="<old>", category="课程")
            with output.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(old))
                writer.writeheader()
                writer.writerow(old)
            original = output.read_bytes()
            duplicate = dict(old, id="different-folder", label="", category="")
            fresh = dict.fromkeys(FIELDS, "")
            fresh.update(id="new", subject="新邮件", sender="Teacher", message_id="<new>")
            self.assertEqual(save_csv([duplicate, fresh, fresh], output, append=True), 1)
            self.assertTrue(output.read_bytes().startswith(original))
            with output.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0], old)
            self.assertEqual(rows[1]["label"], "")
            self.assertEqual(rows[1]["category"], "")
            unchanged = output.read_bytes()
            self.assertEqual(save_csv([duplicate, fresh], output, append=True), 0)
            self.assertEqual(output.read_bytes(), unchanged)

    def test_failed_replace_preserves_original(self):
        with tempfile.TemporaryDirectory(dir=".") as directory:
            output = Path(directory) / "existing.csv"
            row = dict.fromkeys(FIELDS, "")
            row.update(id="1", message_id="<1>", label="1")
            save_csv([row], output)
            original = output.read_bytes()
            with patch("collect_email_headers.os.replace", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    save_csv([dict(row, id="2", message_id="<2>", label="")], output, append=True)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_interactive_new_and_missing_path(self):
        with patch("builtins.input", return_value="new"):
            self.assertFalse(choose_output()[1])
        with patch("builtins.input", return_value="missing-file.csv"):
            with self.assertRaises(ValueError):
                choose_output()


if __name__ == "__main__":
    unittest.main()
