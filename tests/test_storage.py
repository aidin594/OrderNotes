"""Portable data migration; these tests never contact a real SMS gateway."""
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import Database
from storage import portable_data_folder, prepare_portable_storage, program_folder


class PortableStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous = self.root / 'legacy-OrderNotes'
        self.portable = self.root / 'OrderNotes_Portable' / 'data'

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_portable_starts_with_empty_data_directory(self):
        self.assertFalse(prepare_portable_storage(self.portable, self.previous))
        self.assertTrue(self.portable.is_dir())
        self.assertFalse((self.portable / 'order_notes.db').exists())

    def test_migrates_old_database_draft_and_backups_without_deletion(self):
        old = Database(self.previous / 'order_notes.db')
        rid = old.new_request('0701234567', 'Private customer', 'A part', 'Important')
        old.set_settings({'default_sim':'1', 'my_phone':'0709876543'})
        old.begin_sms(rid, '0701234567', 1, 'Hej', 'other')
        (self.previous / 'draft.json').write_text('{"phone":"0700000000"}', encoding='utf-8')
        old.backup_to(self.previous / 'backups' / 'order_notes_2026-10-20.db')
        self.assertTrue(prepare_portable_storage(self.portable, self.previous))
        new = Database(self.portable / 'order_notes.db')
        self.assertEqual(new.get_request(rid)['notes'], 'Important')
        self.assertEqual(new.settings()['my_phone'], '0709876543')
        self.assertEqual(len(new.activity(rid)[1]), 1)
        self.assertTrue((self.portable / 'backups' / 'order_notes_2026-10-20.db').exists())
        self.assertEqual((self.portable / 'draft.json').read_text(encoding='utf-8'), '{"phone":"0700000000"}')
        self.assertTrue((self.previous / 'order_notes.db').is_file())
        self.assertTrue((self.previous / 'draft.json').is_file())

    def test_migration_includes_changes_still_only_in_sqlite_wal(self):
        old = Database(self.previous / 'order_notes.db')
        rid = old.new_request('0701234567', items='before WAL update')
        open_connection = sqlite3.connect(self.previous / 'order_notes.db')
        try:
            open_connection.execute("UPDATE requests SET items='most recent WAL edit' WHERE id=?", (rid,))
            open_connection.commit()
            # Keep the old app's live connection open while taking the snapshot.
            self.assertTrue(prepare_portable_storage(self.portable, self.previous))
            migrated = Database(self.portable / 'order_notes.db')
            self.assertEqual(migrated.get_request(rid)['items'], 'most recent WAL edit')
        finally:
            open_connection.close()

    def test_existing_portable_database_wins_and_never_overwrites(self):
        old = Database(self.previous / 'order_notes.db')
        old.new_request('0701234567', items='old')
        local = Database(self.portable / 'order_notes.db')
        rid = local.new_request('0723456789', items='new')
        self.assertFalse(prepare_portable_storage(self.portable, self.previous))
        self.assertEqual(local.get_request(rid)['items'], 'new')
        self.assertEqual(len(local.requests(filter_name='all')), 1)
        self.assertFalse(prepare_portable_storage(self.portable, self.previous))

    def test_frozen_exe_folder_is_not_internal_resource_folder(self):
        exe = self.root / 'OrderNotes_Portable' / 'OrderNotes.exe'
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)):
            # Windows runners may return a short (8.3) executable path while
            # Path.resolve() expands it to its canonical long-path spelling.
            self.assertEqual(program_folder(), exe.parent.resolve())
            self.assertEqual(portable_data_folder(), exe.parent.resolve() / 'data')

    def test_unwritable_folder_refuses_without_fallback(self):
        with patch('storage.tempfile.NamedTemporaryFile', side_effect=PermissionError('read only')):
            with self.assertRaises(PermissionError):
                prepare_portable_storage(self.portable, self.previous)
        self.assertFalse((self.portable / 'order_notes.db').exists())

    def test_partial_failure_does_not_replace_source_or_create_new_db(self):
        source = Database(self.previous / 'order_notes.db')
        source.new_request('0701234567')
        with patch('storage.sqlite3.connect', side_effect=OSError('broken storage')):
            with self.assertRaises(OSError):
                prepare_portable_storage(self.portable, self.previous)
        self.assertFalse((self.portable / 'order_notes.db').exists())
        self.assertTrue((self.previous / 'order_notes.db').exists())


if __name__ == '__main__':
    unittest.main()
