"""Regression coverage for portable app / per-PC layout / Windows mutex."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from layout_state import (state_path, load_layout, save_layout, main_geometry,
                          dialog_geometry)
from single_instance import (SingleInstance, MUTEX_NAME, ERROR_ALREADY_EXISTS,
                             ERROR_ACCESS_DENIED)


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profile' / 'OrderNotes' / 'ui_layout.json'

    def test_layout_is_per_pc_not_in_portable_database(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': str(Path(self.temp.name) / 'profile')}):
            self.assertEqual(state_path(), self.path)
            self.assertNotIn('OrderNotes_Portable', str(state_path()))
            state = {'main': {'width': 830, 'height': 510, 'x': 12, 'y': 34},
                     'sidebar_width': 240, 'notes_fraction': .63,
                     'dialogs': {'settings': [480, 380]}}
            save_layout(state)
            self.assertEqual(load_layout(), state)
            self.assertEqual(json.loads(self.path.read_text()), state)

    def test_corrupt_layout_is_ignored(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{not-json', encoding='utf-8')
        self.assertEqual(load_layout(self.path), {})
        self.path.write_text('[]', encoding='utf-8')
        self.assertEqual(load_layout(self.path), {})

    def test_saved_window_size_and_position_restored(self):
        layout={'main': {'width': 790, 'height': 520, 'x': 13, 'y': 27}}
        self.assertEqual(main_geometry(layout, 1920, 1080), '790x520+13+27')
        self.assertEqual(main_geometry(layout, 800, 600), '776x520+13+27')
        self.assertEqual(main_geometry(layout, 800, 540), '776x460+13+27')

    def test_offscreen_or_malformed_layout_clamped(self):
        layout={'main': {'width': 99999, 'height': 99999, 'x': 99999, 'y': -500}}
        self.assertEqual(main_geometry(layout, 800, 600), '776x520+24+0')
        self.assertTrue(main_geometry({'main': {'width': 'oops', 'height': None}}, 800, 600)
                        .startswith('776x520'))

    def test_dialog_sizes_saved_and_fit_screen(self):
        layout={'dialogs': {'settings': [620, 500]}}
        self.assertEqual(dialog_geometry(layout, 'settings', (500, 405), (400, 330), (800, 600)), '620x500')
        self.assertEqual(dialog_geometry(layout, 'settings', (500, 405), (400, 330), (600, 480)), '572x395')


class FakeWindowsAPI:
    def __init__(self, handle, error, focus=True):
        self.handle, self.error, self.focus = handle, error, focus
        self.created = []
        self.closed = []
        self.focused = 0
        self.notified = 0

    def create_mutex(self, name):
        self.created.append(name)
        return self.handle, self.error

    def close_handle(self, handle):
        self.closed.append(handle)

    def focus_existing(self):
        self.focused += 1
        return self.focus

    def show_already_running(self):
        self.notified += 1


class SingleInstanceTests(unittest.TestCase):
    def test_first_launch_holds_mutex_until_shutdown(self):
        api=FakeWindowsAPI(123,0)
        guard=SingleInstance(api=api,platform='win32')
        self.assertTrue(guard.acquire())
        self.assertEqual(api.created,[MUTEX_NAME])
        self.assertEqual(api.closed,[])
        guard.close()
        guard.close()
        self.assertEqual(api.closed,[123])

    def test_second_launch_does_not_open_window(self):
        api=FakeWindowsAPI(456,ERROR_ALREADY_EXISTS)
        guard=SingleInstance(api=api,platform='win32')
        self.assertFalse(guard.acquire())
        self.assertEqual(api.closed,[456])
        guard.notify_existing()
        self.assertEqual(api.focused,1)
        self.assertEqual(api.notified,0)

    def test_unfocusable_instance_shows_message(self):
        api=FakeWindowsAPI(None,ERROR_ACCESS_DENIED,focus=False)
        guard=SingleInstance(api=api,platform='win32')
        self.assertFalse(guard.acquire())
        guard.notify_existing()
        self.assertEqual(api.notified,1)

    def test_unexpected_mutex_error_blocks_launch(self):
        api=FakeWindowsAPI(None,6)
        guard=SingleInstance(api=api,platform='win32')
        with self.assertRaises(OSError):
            guard.acquire()

    def test_other_pcs_can_launch_independently(self):
        # A named Windows mutex is local to each PC, not to a network share.
        self.assertTrue(SingleInstance(platform='linux').acquire())


if __name__=='__main__':
    unittest.main()
