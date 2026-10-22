"""Encrypt gateway password for current Windows user using DPAPI (no third-party libs)."""
import base64
import os


def protect(value: str) -> str:
    if not value:
        return ''
    if os.name != 'nt':
        # App targets Windows; non-Windows builds do not persist credentials.
        return ''
    return 'dpapi:' + base64.b64encode(_dpapi(value.encode('utf-8'), True)).decode('ascii')


def unprotect(value: str) -> str:
    if not value or not value.startswith('dpapi:') or os.name != 'nt':
        return ''
    try:
        return _dpapi(base64.b64decode(value[6:]), False).decode('utf-8')
    except (OSError, ValueError, UnicodeError):
        return ''


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]

    data_buffer = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    input_blob = BLOB(len(data), data_buffer)
    output_blob = BLOB()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    protect_fn = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    protect_fn.argtypes = [ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(BLOB)]
    protect_fn.restype = wintypes.BOOL
    if not protect_fn(ctypes.byref(input_blob), None, None, None, None, 0x01, ctypes.byref(output_blob)):
        raise OSError(ctypes.get_last_error(), 'Windows DPAPI failed')
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
