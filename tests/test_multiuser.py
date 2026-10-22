"""Concurrent users of one shared SQLite database, no SMS sent."""
import os
from contextlib import closing
import subprocess
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import Database, ConcurrentUpdateError
from storage import draft_file, backup_suffix, credential_file, is_network_folder


class MultiUserTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'shared'/'order_notes.db'
        self.one=Database(self.path)
        self.two=Database(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rollback_journal_even_for_existing_wal_database(self):
        # sqlite3.Connection as a context manager commits but DOES NOT close.
        # Close explicitly: Windows locks open database files during temp cleanup.
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0],'delete')
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('PRAGMA journal_mode=WAL')
        Database(self.path)
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0],'delete')

    def test_parallel_insertions_no_missing_requests(self):
        errors=[]
        def worker(db, prefix):
            try:
                for i in range(40):
                    db.new_request('0701234567',items=f'{prefix}-{i}')
            except Exception as exc:
                errors.append(exc)
        a=threading.Thread(target=worker,args=(self.one,'A'))
        b=threading.Thread(target=worker,args=(self.two,'B'))
        a.start();b.start();a.join();b.join()
        self.assertEqual(errors,[])
        self.assertEqual(len(self.one.requests(filter_name='all')),80)
        self.assertEqual(len(self.two.requests(filter_name='all')),80)

    def test_two_independent_processes_share_one_database(self):
        # Distinct processes exercise OS file locking rather than just threads.
        code=("import sys; from pathlib import Path; "
              "sys.path.insert(0,sys.argv[2]); from database import Database; "
              "db=Database(Path(sys.argv[1])); "
              "[db.new_request('0701234567',items=str(i)) for i in range(25)]")
        source=str(Path(__file__).resolve().parents[1])
        command=[sys.executable,'-c',code,str(self.path),source]
        first=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        second=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        for process in (first,second):
            out,err=process.communicate(timeout=20)
            self.assertEqual(process.returncode,0,err.decode('utf-8',errors='replace'))
        self.assertEqual(len(self.one.requests(filter_name='all')),50)

    def test_old_editor_cannot_overwrite_new_editor(self):
        rid=self.one.new_request('0701234567',items='initial',notes='secret')
        old=self.one.get_request(rid)
        new_revision=self.two.update_request(rid,'0701234567','','remote change','secret',expected_revision=old['revision'])
        self.assertEqual(new_revision,old['revision']+1)
        with self.assertRaises(ConcurrentUpdateError):
            self.one.update_request(rid,'0701234567','','old change','secret',expected_revision=old['revision'])
        self.assertEqual(self.one.get_request(rid)['items'],'remote change')
        with self.assertRaises(ConcurrentUpdateError):
            self.one.set_stage(rid,'collected',expected_revision=old['revision'])
        self.assertEqual(self.one.get_request(rid)['stage'],'new')
        with self.assertRaises(ConcurrentUpdateError):
            self.one.delete_request(rid,expected_revision=old['revision'])
        self.assertIsNotNone(self.one.get_request(rid))

    def test_reminder_reservation_is_atomic_across_clients(self):
        rid=self.one.new_request('0701234567')
        results=[]
        def worker(db):
            results.append(db.reserve_reminder(rid,'new',None))
        a=threading.Thread(target=worker,args=(self.one,))
        b=threading.Thread(target=worker,args=(self.two,))
        a.start();b.start();a.join();b.join()
        self.assertEqual(sum(v is not None for v in results),1)

    def test_separate_drafts_on_network_and_local_credentials(self):
        home=Path(self.tmp.name)/'OrderNotes'/'data'
        with patch('storage.is_network_folder',return_value=True):
            with patch.dict(os.environ,{'COMPUTERNAME':'DESK1','USERNAME':'user1'}):
                first=draft_file(home)
                suffix=backup_suffix()
            with patch.dict(os.environ,{'COMPUTERNAME':'DESK2','USERNAME':'user2'}):
                second=draft_file(home)
            self.assertNotEqual(first,second)
            self.assertIn('DESK1_user1',str(first))
            self.assertIn('DESK1_user1',suffix)
        self.assertEqual(draft_file(home,shared=False),home/'draft.json')
        self.assertTrue(is_network_folder(Path(r'\\server\share\OrderNotes')))
        with patch.dict(os.environ,{'LOCALAPPDATA':str(self.tmp.name)}):
            self.assertIn(str(self.tmp.name),str(credential_file(home)))


if __name__=='__main__':
    unittest.main()
