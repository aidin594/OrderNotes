"""Windows single-instance guard, independent of the portable/network path.

The named mutex belongs to this Windows PC, not the shared database. This lets
one Order Notes process run on each workstation while preventing duplicate
windows launched from different shortcuts or from local/network copies.
"""
from __future__ import annotations

import sys

MUTEX_NAME = r'Global\OrderNotes.Desktop.SingleInstance'
WINDOW_TITLE = 'Order Notes — Customer requests'
ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5


class WindowsAPI:
    def __init__(self):
        import ctypes
        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        self.kernel.CreateMutexW.restype = ctypes.c_void_p
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int
        self.user.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        self.user.FindWindowW.restype = ctypes.c_void_p
        self.user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.user.IsIconic.argtypes = [ctypes.c_void_p]
        self.user.IsIconic.restype = ctypes.c_int
        self.user.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        self.user.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        self.user.MessageBoxW.restype = ctypes.c_int

    def create_mutex(self, name):
        self.ctypes.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, False, name)
        return handle, self.ctypes.get_last_error()

    def close_handle(self, handle):
        self.kernel.CloseHandle(handle)

    def focus_existing(self):
        hwnd = self.user.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            if self.user.IsIconic(hwnd):
                self.user.ShowWindow(hwnd, 9)  # SW_RESTORE only if minimized
            else:
                self.user.ShowWindow(hwnd, 5)  # SW_SHOW; do not unmaximize
            return bool(self.user.SetForegroundWindow(hwnd))
        return False

    def show_already_running(self):
        self.user.MessageBoxW(None,
            'Order Notes is already open on this computer.\n\nSwitch to the existing window.',
            'Order Notes', 0x40)


class SingleInstance:
    def __init__(self, api=None, platform=None):
        self.platform = sys.platform if platform is None else platform
        self.api = api
        self.handle = None

    def acquire(self) -> bool:
        if self.platform != 'win32':
            return True
        if self.api is None:
            self.api = WindowsAPI()
        handle, error = self.api.create_mutex(MUTEX_NAME)
        if error == ERROR_ALREADY_EXISTS:
            if handle:
                self.api.close_handle(handle)
            return False
        if not handle and error == ERROR_ACCESS_DENIED:
            # Another Windows login owns the mutex; do not start a duplicate.
            return False
        if not handle:
            raise OSError(error, 'Cannot establish Order Notes single-instance lock')
        self.handle = handle
        return True

    def notify_existing(self):
        if self.api and not self.api.focus_existing():
            self.api.show_already_running()

    def close(self):
        if self.handle is not None:
            self.api.close_handle(self.handle)
            self.handle = None
