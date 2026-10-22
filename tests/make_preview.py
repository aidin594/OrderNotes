"""Create an optional UI preview using synthetic sample records only.

Requires Pillow and a desktop display. No real customer database is opened.
Usage: python tests/make_preview.py [output.png]
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with tempfile.TemporaryDirectory() as temporary:
    os.environ['LOCALAPPDATA'] = temporary
    import app
    import tkinter as tk
    from PIL import ImageGrab

    app.HOME = Path(temporary) / 'OrderNotes'
    root = tk.Tk()
    root.geometry('800x600+0+0')
    ui = app.OrderNotesApp(root)
    ui.db.new_request('0701234567', 'Demo customer', 'Bike brake cable', 'Ask for the black version')
    ui.db.new_request('0709876543', 'Another customer', 'Wheel spoke 286mm', 'Urgent')
    ui.set_filter('all')
    ui.show_request(ui.db.requests(filter_name='all')[0]['id'])
    root.update()
    ui._restore_split_positions()
    root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / 'OrderNotes_Preview.png'
    ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(output)
    print(output)
    with patch.object(app.messagebox, 'askyesno', return_value=True):
        ui.close()
