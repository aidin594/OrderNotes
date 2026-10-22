"""Thread-safe worker callbacks and shutdown regression tests."""
import sys
import threading
import unittest
from pathlib import Path
from queue import SimpleQueue
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import OrderNotesApp


class DummyRoot:
    def __init__(self):
        self.main_thread=threading.get_ident()
        self.scheduled=[]
    def after(self, delay, callback):
        assert threading.get_ident()==self.main_thread, 'Tk called off main thread'
        self.scheduled.append((delay,callback))


class WorkerDispatchTests(unittest.TestCase):
    def make_app(self):
        ui=object.__new__(OrderNotesApp)
        ui._closing=False
        ui._ui_events=SimpleQueue()
        ui._pending_after_ids=set()
        ui.root=DummyRoot()
        return ui

    def test_background_thread_only_enqueues_and_main_thread_dispatches(self):
        ui=self.make_app()
        calls=[]
        t=threading.Thread(target=lambda:ui._post_ui(lambda:calls.append('ok')))
        t.start(); t.join(timeout=2)
        self.assertFalse(t.is_alive())
        self.assertEqual(calls,[])
        self.assertEqual(ui.root.scheduled,[])
        ui._drain_ui_events()
        self.assertEqual(calls,['ok'])
        self.assertEqual(len(ui.root.scheduled),1)

    def test_reminder_worker_can_finish_after_close_without_touching_tk(self):
        ui=self.make_app()
        class DB:
            def settings(self):
                return {'my_phone': ''}
        ui.db=DB()
        ui.gateway_password=''
        ui._reminder_lock=threading.Lock()
        started=threading.Event()
        release=threading.Event()
        def fake_reminder(*args):
            started.set()
            self.assertTrue(release.wait(2))
        with patch('app.process_due_reminders',side_effect=fake_reminder):
            ui._reminder_tick()
            self.assertTrue(started.wait(2))
            ui._closing=True
            release.set()
            ui._reminder_thread.join(timeout=2)
            self.assertFalse(ui._reminder_thread.is_alive())
        self.assertTrue(ui._ui_events.empty())

    def test_after_close_worker_result_is_ignored(self):
        ui=self.make_app()
        ui._closing=True
        t=threading.Thread(target=lambda:ui._post_ui(lambda:1/0))
        t.start(); t.join(timeout=2)
        self.assertFalse(t.is_alive())
        ui._drain_ui_events()
        self.assertEqual(ui.root.scheduled,[])


if __name__=='__main__':
    unittest.main()
