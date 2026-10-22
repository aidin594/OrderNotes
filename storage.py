"""Portable on-disk storage: beside the executable, never in a build directory.

The app deliberately does not write into PyInstaller's _MEIPASS/_internal
folder: those contain bundled resources and can be replaced on upgrades.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def program_folder() -> Path:
    """Executable folder for a frozen build; source-code folder otherwise."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def portable_data_folder() -> Path:
    return program_folder() / 'data'


def previous_data_folder() -> Path:
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'OrderNotes'


def _copy_without_clobber(source: Path, target: Path) -> None:
    """Copy an auxiliary file once, atomically; never replace portable edits."""
    if target.exists() or not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tempname = tempfile.mkstemp(prefix='.copy-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as dst, source.open('rb') as src:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if not target.exists():
            os.replace(tempname, target)
            tempname = None
    finally:
        if tempname is not None:
            os.unlink(tempname)


def prepare_portable_storage(home: Path | None = None, previous: Path | None = None) -> bool:
    """Initialize writable portable data, migrating an old profile if needed.

    Returns True if an old SQLite database was copied. A live SQLite backup is
    used to capture uncheckpointed WAL changes. Legacy files are kept intact.
    Existing portable DB always wins and is never overwritten.
    """
    home = Path(home) if home is not None else portable_data_folder()
    previous = Path(previous) if previous is not None else previous_data_folder()
    home.mkdir(parents=True, exist_ok=True)

    # Verify writable now, rather than losing autosaves later (Program Files).
    with tempfile.NamedTemporaryFile(prefix='.writecheck-', dir=home) as probe:
        probe.write(b'ok')
        probe.flush()

    # Older versions used one shared draft.json. Keep it and copy it once
    # into this PC's own draft slot if opening from a network share.
    if is_network_folder(home) and (home / 'draft.json').is_file():
        _copy_without_clobber(home / 'draft.json', draft_file(home, shared=True))

    target_db = home / 'order_notes.db'
    source_db = previous / 'order_notes.db'
    if target_db.exists() or not source_db.is_file() or home.resolve() == previous.resolve():
        return False

    # Preserve backups and any unsaved draft before committing the migrated DB.
    if (previous / 'draft.json').is_file():
        _copy_without_clobber(previous / 'draft.json', home / 'draft.json')
    old_backups = previous / 'backups'
    if old_backups.is_dir():
        for old_backup in old_backups.glob('order_notes_????-??-??.db'):
            _copy_without_clobber(old_backup, home / 'backups' / old_backup.name)

    fd, temporary = tempfile.mkstemp(prefix='.db-migrate-', suffix='.db', dir=home)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source_db.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as src, \
             closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            dst.commit()
        with closing(sqlite3.connect(temporary)) as verify:
            if verify.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise sqlite3.DatabaseError('Migrated database integrity check failed')
            tables = {row[0] for row in verify.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'requests', 'settings', 'sms_log'}.issubset(tables):
                raise sqlite3.DatabaseError('Previous database is not an Order Notes database')
        if not target_db.exists():
            os.replace(temporary, target_db)
            temporary = None
        return True
    finally:
        if temporary is not None:
            os.unlink(temporary)


def is_network_folder(folder: Path | None = None) -> bool:
    """Detect UNC and mapped Windows network drives (including SMB shares)."""
    folder=Path(folder) if folder is not None else program_folder()
    name=str(folder)
    if name.startswith('\\\\') or name.startswith('//'):
        return True
    if sys.platform!='win32':
        return False
    try:
        import ctypes
        root=folder.anchor
        if not root:
            return False
        # DRIVE_REMOTE = 4; Windows drive-letter mappings use this.
        return ctypes.windll.kernel32.GetDriveTypeW(str(root))==4
    except (OSError,AttributeError):
        return False


def _safe_pc_name() -> str:
    import re
    name=(os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME') or 'PC')
    user=(os.environ.get('USERNAME') or os.environ.get('USER') or 'user')
    return re.sub(r'[^A-Za-z0-9_-]', '_', name+'_'+user)[:70]


def draft_file(home: Path | None = None, shared: bool | None = None) -> Path:
    home=Path(home) if home is not None else portable_data_folder()
    if shared is None:
        shared=is_network_folder(home)
    return home / 'drafts' / (_safe_pc_name()+'.json') if shared else home / 'draft.json'


def backup_suffix(shared: bool | None = None) -> str:
    if shared is None:
        shared=is_network_folder()
    return '_'+_safe_pc_name() if shared else ''


def credential_file(home: Path | None = None) -> Path:
    """DPAPI ciphertext is user-bound; never share it as a global setting."""
    import hashlib
    home=Path(home) if home is not None else portable_data_folder()
    fingerprint=hashlib.sha256(str(home).lower().encode('utf-8')).hexdigest()[:20]
    profile=Path(os.environ.get('LOCALAPPDATA') or (Path.home()/'.local'/'share'))
    return profile/'OrderNotes'/'secrets'/(fingerprint+'.json')
