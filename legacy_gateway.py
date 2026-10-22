"""Optional migration of the user's own saved gateway credential from Windows.

Only reads the exact SMSGateDeskCommunity credential for the user's configured
local gateway; it never enumerates credentials, logs them, or adds them to builds.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


class _FILETIME(ctypes.Structure):
    _fields_ = [('low', wintypes.DWORD), ('high', wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
        ('LastWritten', _FILETIME), ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
        ('Persist', wintypes.DWORD), ('AttributeCount', wintypes.DWORD),
        ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR),
        ('UserName', wintypes.LPWSTR),
    ]


def read_old_gateway_password(server: str) -> str | None:
    """Read user's own SMSGate Desktop Community credential, if present."""
    if sys.platform != 'win32' or not server:
        return None
    try:
        dll = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        dll.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
        dll.CredReadW.restype = wintypes.BOOL
        dll.CredFree.argtypes = [ctypes.c_void_p]
        dll.CredFree.restype = None
        ptr = ctypes.POINTER(_CREDENTIALW)()
        target = 'SMSGateDeskCommunity:' + server.strip().rstrip('/')
        if not dll.CredReadW(target, 1, 0, ctypes.byref(ptr)):
            return None
        try:
            entry = ptr.contents
            blob = ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize)
            enc = 'utf-16' if blob.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-16-le'
            return blob.decode(enc).rstrip('\x00') if blob else None
        finally:
            dll.CredFree(ctypes.cast(ptr, ctypes.c_void_p))
    except (OSError, UnicodeError, ValueError, AttributeError):
        return None
