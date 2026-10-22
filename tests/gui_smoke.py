"""Integration smoke test for compact, scrollbar-free layouts (no real SMS)."""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
with tempfile.TemporaryDirectory() as td:
    os.environ['LOCALAPPDATA']=td
    import app
    import tkinter as tk
    from tkinter import ttk
    app.HOME=Path(td)/'OrderNotes'
    root=tk.Tk()
    ui=app.OrderNotesApp(root)
    root.update()
    assert ui.selected_id is None
    assert ui.save_button.winfo_ismapped()
    assert ui.settings['gateway_url']==''
    assert ui.settings['gateway_user']==''
    assert ui.settings['default_sim']=='1'
    ui.phone_var.set('0701234567')
    ui.items_box.insert('1.0','Shimano Nexus 7 reglage')
    root.update()
    assert ui.save_new()
    assert ui.selected_id is None
    assert ui.phone_var.get()==''
    assert app.text_get(ui.items_box)==''
    assert not (app.HOME/'draft.json').exists()
    rid=ui.db.requests(filter_name='all')[0]['id']
    ui.show_request(rid)
    root.update()
    assert ui.delete_button.winfo_ismapped()
    assert len(ui.notes_split.panes())==2, 'notes and items must have a draggable splitter'
    assert len(ui.main_split.panes())==2, 'sidebar must have a draggable divider'
    sash_before=ui.notes_split.sash_coord(0)[1]
    ui.notes_split.sash_place(0,0,sash_before+12)
    root.update()
    assert ui.notes_split.sash_coord(0)[1]!=sash_before
    # Windows-style right-click actions are available on each request row.
    from types import SimpleNamespace
    ui.show_request_menu(SimpleNamespace(x_root=60,y_root=120),rid)
    assert ui.request_menu.entrycget(3,'label')=='Copy phone number'
    assert ui.request_menu.entrycget('end','label')=='Delete request...'
    ui.request_menu.unpost()
    # Visual hierarchy: blue primary, red destructive, distinct neutral controls.
    assert ui.send_btn.cget('bg')==app.COLORS['blue']
    assert ui.delete_button.cget('bg')==app.COLORS['red_light']
    assert ui.change_status_button.cget('bg')==app.COLORS['gray_light']
    assert ui.send_btn.cget('bg')!=ui.change_status_button.cget('bg')
    ui.set_filter('all')
    assert ui.filter_buttons['all'].cget('bg')==app.COLORS['blue']
    assert ui.filter_buttons['all'].cget('fg')==app.COLORS['white']
    assert ui.filter_buttons['to_notify'].cget('bg')==app.COLORS['gray_light']
    # Stage actions are color-coded but do not change functionality.
    stage_colors=[w.cget('bg') for w in ui.quick_actions.winfo_children() if isinstance(w,tk.Button)]
    assert app.COLORS['blue_light'] in stage_colors
    assert app.COLORS['amber_light'] in stage_colors
    assert app.COLORS['green_light'] in stage_colors
    assert ui.db.get_request(rid)['phone']=='0701234567'
    assert len(ui.nb.tabs())==3
    assert not any(isinstance(w,(tk.Scrollbar,ttk.Scrollbar,tk.Canvas)) for w in ui.editor_inner.winfo_children())
    assert not any(isinstance(w,(tk.Scrollbar,ttk.Scrollbar,tk.Canvas)) for w in ui.list_inner.winfo_children())
    ui.mark_stage('ordered')
    ui.mark_stage('arrived')
    assert ui.db.get_request(rid)['stage']=='arrived'
    # Populate enough records that sidebar pagination is required.
    for i in range(18):
        ui.db.new_request('0701234567',items=f'Request item {i}')
    ui.set_filter('all')
    root.geometry('760x480')
    root.update()
    ui.refresh_list()
    assert ui.next_list.cget('state')=='normal', 'pagination must replace scrolling'
    p=ui.list_page
    ui.change_list_page(1)
    assert ui.list_page==p+1
    assert not ui.save_button.winfo_ismapped()
    # Activity pagination instead of a scrollbar.
    ui.show_request(rid)
    ui._render_history()
    assert len(ui.activity_pages)>1
    prev=ui.activity_page
    ui.change_activity_page(1)
    assert ui.activity_page==prev+1
    # Cancel delete leaves all records untouched.
    with patch.object(app.messagebox,'askyesno',return_value=False):
        ui.delete_selected_request()
    assert ui.db.get_request(rid) is not None
    with patch.object(app.messagebox,'askyesno',return_value=True):
        ui.delete_selected_request()
    root.update()
    assert ui.selected_id is None
    assert ui.db.get_request(rid) is None
    assert ui.phone_var.get()==''
    # Dialogs fit without screen-height scrollbars.
    for opener in (ui.open_settings,ui.edit_templates,ui.show_help):
        opener()
        root.update()
        dialogs=[w for w in root.winfo_children() if isinstance(w,tk.Toplevel)]
        assert dialogs
        for dlg in dialogs:
            assert dlg.winfo_height() <= root.winfo_screenheight()
            if dlg.title().endswith('Settings'):
                dlg.geometry('470x370')
                root.update()
            elif dlg.title()=='SMS templates':
                dlg.geometry('590x400')
                root.update()
            dlg.destroy()
    assert ui.ui_layout['dialogs']['settings']==[470,370], ui.ui_layout
    assert ui.ui_layout['dialogs']['templates']==[590,400], ui.ui_layout
    if '--screenshot' in sys.argv:
        # Screenshots are produced with a synthetic request in a fresh profile.
        ui.show_request(ui.db.requests(filter_name='all')[0]['id'])
        root.update()
        from PIL import ImageGrab
        ImageGrab.grab().save('/mnt/data/OrderNotes_v1.4_NoScroll_Preview.png')
    # Window, sidebar and notes split must survive a normal exit and restart.
    target_width=min(800,max(580,root.winfo_screenwidth()-24))
    target_height=min(520,max(430,root.winfo_screenheight()-80))
    root.geometry(f'{target_width}x{target_height}+0+0')
    root.update()
    ui._restore_split_positions()
    root.update()
    ui.main_split.sash_place(0,250,0)
    ui.notes_split.sash_place(0,0,round(ui.notes_split.winfo_height()*.65))
    root.update()
    expected_sidebar=ui.main_split.sash_coord(0)[0]
    expected_fraction=ui.notes_split.sash_coord(0)[1]/ui.notes_split.winfo_height()
    # Cancel exit: the application and all data must remain available.
    with patch.object(app.messagebox,'askyesno',return_value=False) as confirm:
        ui.close()
        assert root.winfo_exists()
        assert confirm.call_count==1
    with patch.object(app.messagebox,'askyesno',return_value=True):
        ui.close()
    from layout_state import load_layout
    saved=load_layout()
    assert saved['main']['width']==target_width, saved
    assert saved['main']['height']==target_height, saved
    assert abs(saved['sidebar_width']-expected_sidebar)<=2, saved
    assert abs(saved['notes_fraction']-expected_fraction)<.02, saved
    # Restart restores the persisted layout without touching the customer DB.
    root2=tk.Tk()
    ui2=app.OrderNotesApp(root2)
    root2.update()
    # Windows may need a few mapped-window geometry passes. Verify the
    # restored sizes after the UI has settled, not on its first paint.
    deadline=time.monotonic()+1.5
    while time.monotonic()<deadline:
        root2.update()
        if (root2.winfo_width()==target_width and
            root2.winfo_height()==target_height and
            abs(ui2.main_split.sash_coord(0)[0]-expected_sidebar)<=3 and
            ui2.notes_split.winfo_height()>130 and
            abs(ui2.notes_split.sash_coord(0)[1]/ui2.notes_split.winfo_height()-expected_fraction)<.04):
            break
        time.sleep(.03)
    assert root2.winfo_width()==target_width
    assert root2.winfo_height()==target_height
    assert abs(ui2.main_split.sash_coord(0)[0]-expected_sidebar)<=3
    assert abs(ui2.notes_split.sash_coord(0)[1]/ui2.notes_split.winfo_height()-expected_fraction)<.04, (ui2.notes_split.sash_coord(0),ui2.notes_split.winfo_height(),expected_fraction)
    # Regression: Windows/Tk can provide only 190px for the editors at
    # 800x520. Previously minsize=65 clamped a 65% split to 115/190 (60.5%).
    # Verify the real editor panes can still place a 65% divider at 190px.
    compact=tk.Toplevel(root2)
    compact.geometry('380x190+0+0')
    compact_split=tk.PanedWindow(compact,orient='vertical',sashwidth=7,
                                 showhandle=True,handlesize=12,handlepad=8,bd=0)
    compact_split.pack(fill='both',expand=True)
    for original_pane in ui2.notes_split.panes():
        minsize=int(ui2.notes_split.panecget(original_pane,'minsize'))
        pane=tk.Frame(compact_split)
        tk.Label(pane,text='ITEMS / NOTES').pack(anchor='w')
        tk.Text(pane,height=3).pack(fill='both',expand=True)
        compact_split.add(pane,minsize=minsize,stretch='always')
    compact.update()
    assert compact_split.winfo_height()==190, compact_split.winfo_height()
    compact_split.sash_place(0,0,round(compact_split.winfo_height()*.65))
    compact.update()
    assert abs(compact_split.sash_coord(0)[1]/190-.65)<.04, (
        compact_split.sash_coord(0),compact_split.winfo_height())
    compact.destroy()
    with patch.object(app.messagebox,'askyesno',return_value=True):
        ui2.close()
    from threading import Thread
    t=Thread(target=lambda:ui2._post_ui(lambda:1/0))
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()
print('GUI smoke test passed (including layout restart, worker shutdown and exit confirmation)')
