"""Per-Windows-user interface layout, kept OFF the shared customer database.

The portable database belongs beside the EXE; window geometry does not.
Different workstations opening the same network folder should be free to use
independent window sizes, sidebar widths and note-editor split ratios.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def state_path() -> Path:
    profile = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / '.local' / 'share'))
    return profile / 'OrderNotes' / 'ui_layout.json'


def load_layout(path: Path | None = None) -> dict:
    try:
        value = json.loads((path or state_path()).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_layout(value: dict, path: Path | None = None) -> None:
    target = Path(path or state_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.ui-layout-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _bounded_int(value, minimum: int, maximum: int, default: int) -> int:
    if type(value) is not int:  # reject bool, float, malformed JSON
        value = default
    return max(minimum, min(maximum, value))


def main_geometry(layout: dict, screen_width: int, screen_height: int) -> str:
    """Fit saved geometry on the current display, including after unplugging a monitor."""
    saved = layout.get('main', {})
    if not isinstance(saved, dict):
        saved = {}
    available_width = max(580, screen_width - 24)
    available_height = max(430, screen_height - 80)
    default_width = min(970, available_width)
    default_height = min(600, available_height)
    width = _bounded_int(saved.get('width'), min(690, available_width), available_width, default_width)
    height = _bounded_int(saved.get('height'), min(440, available_height), available_height, default_height)
    if type(saved.get('x')) is int and type(saved.get('y')) is int:
        x = _bounded_int(saved['x'], 0, max(0, screen_width - width), 0)
        y = _bounded_int(saved['y'], 0, max(0, screen_height - height), 0)
        return f'{width}x{height}+{x}+{y}'
    return f'{width}x{height}'


def dialog_geometry(layout: dict, name: str, default: tuple[int, int],
                    minimum: tuple[int, int], screen: tuple[int, int]) -> str:
    dialogs = layout.get('dialogs', {})
    saved = dialogs.get(name, []) if isinstance(dialogs, dict) else []
    if not isinstance(saved, (list, tuple)) or len(saved) != 2:
        saved = default
    max_width = max(300, screen[0] - 28)
    max_height = max(260, screen[1] - 85)
    width = _bounded_int(saved[0], min(minimum[0], max_width), max_width, min(default[0], max_width))
    height = _bounded_int(saved[1], min(minimum[1], max_height), max_height, min(default[1], max_height))
    return f'{width}x{height}'
