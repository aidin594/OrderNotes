"""Order Notes — fast, offline-first Windows order notebook with SMSGate support."""
from __future__ import annotations

import csv
import json
import sqlite3
import os
import sys
import threading
from queue import Empty, SimpleQueue
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from database import Database, ConcurrentUpdateError, STAGES, STAGE_LABELS, now
from gateway import Gateway, GatewayError, check_url
from phone import normalized_phone, valid_phone
from reminders import process_due_reminders
from security import protect, unprotect
from legacy_gateway import read_old_gateway_password
from sms_utils import estimate_sms
from storage import portable_data_folder, prepare_portable_storage, draft_file, backup_suffix, credential_file, is_network_folder
from layout_state import load_layout, save_layout, main_geometry, dialog_geometry
from single_instance import SingleInstance, WINDOW_TITLE

APP_NAME = 'Order Notes'
HOME = portable_data_folder()

# High-contrast light palette: strong actions, visible secondary buttons, clear selection.
# Keep the palette centralized so all dialogs share the same appearance.
COLORS = {
    'bg': '#EEF2F7', 'white': '#FFFFFF', 'ink': '#17243A', 'muted': '#51637C',
    'line': '#C9D3E1', 'blue': '#155EEF', 'blue_hover': '#0D49BC',
    'blue_light': '#DBEAFE', 'green': '#116B47', 'green_light': '#D9F4E6',
    'amber': '#8B4D08', 'amber_light': '#FFF0CE',
    'gray_light': '#E5EBF3', 'neutral_hover': '#CED9E8',
    'blue_soft_hover': '#C1D8FC', 'amber_hover': '#F9DFA3',
    'green_hover': '#B8E8CD',
    'red': '#B42318', 'red_light': '#FEE4E2', 'red_hover': '#FBC9C4',
}
BADGES = {
    'new': (COLORS['amber_light'],COLORS['amber']),
    'ordered': (COLORS['blue_light'],COLORS['blue']),
    'arrived': (COLORS['amber_light'],COLORS['amber']),
    'notified': (COLORS['blue_light'],COLORS['blue']),
    'collected': (COLORS['green_light'],COLORS['green']),
}


def fmt_timestamp(stamp):
    if not stamp:
        return '—'
    try:
        return datetime.fromisoformat(stamp).strftime('%d %b %Y · %H:%M')
    except ValueError:
        return str(stamp)


def set_button_colors(widget, bg, fg, hover):
    """Update base, hover and pressed colors together (also for selected filters)."""
    widget._button_colors=(bg,fg,hover)
    widget.configure(bg=bg,fg=fg,activebackground=hover,activeforeground=fg,
                     disabledforeground=COLORS['muted'])


def button(parent, text, cmd, primary=False, small=False, danger=False, tone=None, **kw):
    if primary:
        bg,fg,hover=COLORS['blue'],COLORS['white'],COLORS['blue_hover']
    elif danger:
        bg,fg,hover=COLORS['red_light'],COLORS['red'],COLORS['red_hover']
    elif tone in ('ordered','arrived','collected'):
        bg,fg,hover={
            'ordered':(COLORS['blue_light'],COLORS['blue'],COLORS['blue_soft_hover']),
            'arrived':(COLORS['amber_light'],COLORS['amber'],COLORS['amber_hover']),
            'collected':(COLORS['green_light'],COLORS['green'],COLORS['green_hover']),
        }[tone]
    else:
        bg,fg,hover=COLORS['gray_light'],COLORS['ink'],COLORS['neutral_hover']
    widget=tk.Button(parent,text=text,command=cmd,bg=bg,fg=fg,
                     activebackground=hover,activeforeground=fg,
                     relief='flat',bd=0,highlightthickness=0,
                     cursor='hand2',font=('Segoe UI',9 if small else 10,
                                         'bold' if primary or danger or tone else 'normal'),
                     padx=9 if small else 12,pady=5 if small else 7,**kw)
    set_button_colors(widget,bg,fg,hover)
    def hover_enter(_):
        if str(widget.cget('state'))=='normal':
            widget.configure(bg=widget._button_colors[2])
    def hover_leave(_):
        widget.configure(bg=widget._button_colors[0])
    widget.bind('<Enter>',hover_enter,add='+')
    widget.bind('<Leave>',hover_leave,add='+')
    return widget


def label(parent, text='', size=10, bold=False, fg=None, bg=None, **kw):
    return tk.Label(parent,text=text,font=('Segoe UI',size,'bold' if bold else 'normal'),
                    bg=bg or COLORS['white'],fg=fg or COLORS['ink'],**kw)


def entry(parent, textvariable=None, width=None):
    return tk.Entry(parent,textvariable=textvariable,bg=COLORS['white'],fg=COLORS['ink'],
                    insertbackground=COLORS['ink'],font=('Segoe UI',10),relief='solid',bd=1,
                    highlightthickness=1,highlightbackground=COLORS['line'],highlightcolor=COLORS['blue'],
                    width=width or 20)


def text_box(parent,height=3):
    return tk.Text(parent,height=height,wrap='word',undo=True,font=('Segoe UI',10),
                   bg=COLORS['white'],fg=COLORS['ink'],insertbackground=COLORS['ink'],
                   padx=7,pady=5,relief='solid',bd=1,highlightthickness=1,
                   highlightcolor=COLORS['blue'],highlightbackground=COLORS['line'])


def text_get(widget):
    return widget.get('1.0','end-1c')


def set_text(widget, value):
    widget.delete('1.0','end')
    widget.insert('1.0',value or '')


def save_json_atomic(path: Path, obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    with open(temp,'w',encoding='utf-8') as f:
        json.dump(obj,f,ensure_ascii=False,indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp,path)


def keep_canvas_content_at_top(canvas, inner):
    """Avoid Tk canvas overscrolling when the content is shorter than its viewport.

    Tk permits a negative canvas origin with a scrollregion shorter than the
    viewport, so using only bbox('all') can move the form down into white space.
    Give the scrollregion at least the viewport height and reset any stale
    offset as soon as the whole document fits.
    """
    view_height=max(1, canvas.winfo_height())
    view_width=max(1, canvas.winfo_width())
    content_height=inner.winfo_reqheight()
    canvas.configure(scrollregion=(0, 0, view_width, max(view_height, content_height)))
    if content_height<=view_height:
        canvas.yview_moveto(0)


def wheel_scroll_if_needed(canvas, inner, event):
    if inner.winfo_reqheight() > canvas.winfo_height():
        canvas.yview_scroll(int(-event.delta/120), 'units')
    return 'break'


def load_draft():
    try:
        return json.loads(draft_file(HOME).read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return {}


def read_local_gateway_password():
    try:
        data=json.loads(credential_file(HOME).read_text(encoding='utf-8'))
        return unprotect(data.get('password',''))
    except (OSError,ValueError,TypeError):
        return ''


def store_local_gateway_password(plaintext):
    encrypted=protect(plaintext)
    if plaintext and not encrypted:
        raise ValueError('Secure password storage requires Windows.')
    save_json_atomic(credential_file(HOME),{'password':encrypted})


def set_launch_at_login(enabled: bool):
    if sys.platform!='win32':
        return
    import winreg
    key=r'Software\Microsoft\Windows\CurrentVersion\Run'
    if getattr(sys,'frozen',False):
        cmd=f'"{sys.executable}"'
    else:
        py=sys.executable
        if py.lower().endswith('python.exe'):
            py=py[:-10]+'pythonw.exe'
        cmd=f'"{py}" "{Path(__file__).resolve()}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,key,0,winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k,'OrderNotes',0,winreg.REG_SZ,cmd)
        else:
            try:
                winreg.DeleteValue(k,'OrderNotes')
            except FileNotFoundError:
                pass


class OrderNotesApp:
    def __init__(self,root):
        self.root=root
        self.db=Database(HOME/'order_notes.db')
        self.db.prefill_existing_gateway()
        self.selected_id=None
        self.selected_revision=None
        self.edit_conflict=False
        self.list_page=0
        self._list_size=(0,0)
        self._layout_refresh_queued=False
        self.activity_page=0
        self.activity_pages=[]
        self.filter='to_notify'
        self.loading=False
        self.autosave_after=None
        self.refresh_after=None
        self._reminder_lock=threading.Lock()
        # Worker threads must NEVER call Tk (even root.after) on Windows.
        self._ui_events=SimpleQueue()
        self._closing=False
        self._pending_after_ids=set()
        self._split_touched=False
        self._split_verify_count=0
        self.sending=False
        self.settings=self.db.settings()
        self.ui_layout=load_layout()  # Stored per PC, not in the shared database.
        saved_main=self.ui_layout.get('main',{})
        self._last_normal_main=dict(saved_main) if isinstance(saved_main,dict) else {}
        self.gateway_password=read_local_gateway_password()
        if not self.gateway_password:
            self.gateway_password=unprotect(self.settings.get('gateway_password',''))
            if self.gateway_password:
                store_local_gateway_password(self.gateway_password)
        if not self.gateway_password:
            old_password=read_old_gateway_password(self.settings.get('gateway_url',''))
            if old_password:
                protected=protect(old_password)
                if protected:
                    self.gateway_password=old_password
                    store_local_gateway_password(old_password)
        self.templates=[]
        self.template_names=[]
        self._setup_window()
        self._build_ui()
        self.refresh_templates()
        self.new_request()
        self.refresh_list()
        self._daily_backup()
        self._after(100,self._drain_ui_events)
        self._after(1300,self._reminder_tick)
        self._after(5000,self._shared_refresh)
        self._after_idle(self._restore_split_positions)
        # Windows can reposition Tk paned sashes during the first mapped
        # geometry passes. Recheck a few times, unless the user has dragged.
        self._after(160,self._verify_initial_split_positions)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<Configure>',self._remember_normal_window,add='+')
        self.root.bind('<Control-n>',lambda event:self.new_request())
        self.root.bind('<Control-f>',lambda event:self.focus_search())
        self.root.bind('<Control-s>',lambda event:self.save_new() if self.selected_id is None else self._flush())

    def _after(self, delay_ms, callback):
        """Track Tk callbacks so shutdown cancels them before destroying widgets."""
        if self._closing:
            return None
        token=None
        def run():
            self._pending_after_ids.discard(token)
            if not self._closing:
                callback()
        token=self.root.after(delay_ms,run)
        self._pending_after_ids.add(token)
        return token

    def _after_idle(self, callback):
        if self._closing:
            return None
        token=None
        def run():
            self._pending_after_ids.discard(token)
            if not self._closing:
                callback()
        token=self.root.after_idle(run)
        self._pending_after_ids.add(token)
        return token

    def _cancel_after(self, token):
        if token is None:
            return
        self._pending_after_ids.discard(token)
        try:
            self.root.after_cancel(token)
        except tk.TclError:
            pass

    def _setup_window(self):
        self.root.title(WINDOW_TITLE)
        self.root.configure(bg=COLORS['bg'])
        # Restore this PC's previous window dimensions; clamp off-screen values.
        screen_w=self.root.winfo_screenwidth()
        screen_h=self.root.winfo_screenheight()
        self.root.geometry(main_geometry(self.ui_layout,screen_w,screen_h))
        self.root.minsize(min(690,max(580,screen_w-24)),min(440,max(430,screen_h-80)))
        saved_main=self.ui_layout.get('main',{})
        if isinstance(saved_main,dict) and saved_main.get('maximized') and sys.platform=='win32':
            self._after(65,lambda:self.root.state('zoomed'))
        style=ttk.Style(self.root)
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure('Modern.TCombobox',font=('Segoe UI',9),padding=2,fieldbackground='white')
        style.configure('Modern.TNotebook',background=COLORS['bg'],borderwidth=0)
        style.configure('Modern.TNotebook.Tab',font=('Segoe UI',9,'bold'),padding=(10,4),
                        background=COLORS['gray_light'],foreground=COLORS['ink'])
        style.map('Modern.TNotebook.Tab',
                  background=[('selected',COLORS['blue']),('active',COLORS['neutral_hover'])],
                  foreground=[('selected',COLORS['white']),('active',COLORS['ink'])])
        self.root.option_add('*Font','{Segoe UI} 9')

    def _remember_normal_window(self,event):
        if event.widget is not self.root or self.root.state()!='normal':
            return
        if event.width>=580 and event.height>=400:
            self._last_normal_main.update(width=event.width,height=event.height,
                                          x=self.root.winfo_x(),y=self.root.winfo_y())

    def _restore_split_positions(self):
        # Tk has to finish the initial geometry pass before a sash can move.
        if not self.root.winfo_exists():
            return
        split=self.main_split
        width=split.winfo_width()
        height=self.notes_split.winfo_height()
        if width < 550 or height < 130:
            self._after(100,self._restore_split_positions)
            return
        sidebar=self.ui_layout.get('sidebar_width')
        if type(sidebar) is not int:
            sidebar=max(218,min(290,int(self.root.winfo_width()*.29)))
        sidebar=max(210,min(sidebar,width-335))
        split.sash_place(0,sidebar,0)
        # Finish the horizontal pane geometry before sizing the nested vertical
        # panes. Otherwise Windows may recalculate the notes sash afterwards.
        self.root.update_idletasks()
        height=self.notes_split.winfo_height()
        ratio=self.ui_layout.get('notes_fraction',0.5)
        if not isinstance(ratio,(float,int)) or isinstance(ratio,bool):
            ratio=0.5
        ratio=max(0.18,min(0.82,float(ratio)))
        self.notes_split.sash_place(0,0,round(height*ratio))

    def _verify_initial_split_positions(self):
        if self._closing or self._split_touched or self._split_verify_count>=3:
            return
        self._split_verify_count+=1
        width=self.main_split.winfo_width()
        height=self.notes_split.winfo_height()
        if width>=550 and height>=130:
            sidebar=self.ui_layout.get('sidebar_width')
            if type(sidebar) is not int:
                sidebar=max(218,min(290,int(self.root.winfo_width()*.29)))
            sidebar=max(210,min(sidebar,width-335))
            ratio=self.ui_layout.get('notes_fraction',0.5)
            if not isinstance(ratio,(float,int)) or isinstance(ratio,bool):
                ratio=0.5
            ratio=max(.18,min(.82,float(ratio)))
            if (abs(self.main_split.sash_coord(0)[0]-sidebar)>3 or
                abs(self.notes_split.sash_coord(0)[1]/height-ratio)>.025):
                self._restore_split_positions()
        if self._split_verify_count<3 and not self._split_touched:
            self._after(130,self._verify_initial_split_positions)

    def _capture_layout(self):
        state=self.root.state()
        previous=self.ui_layout.get('main',{})
        if not isinstance(previous,dict):
            previous={}
        if state=='normal':
            previous.update(width=self.root.winfo_width(),height=self.root.winfo_height(),
                            x=self.root.winfo_x(),y=self.root.winfo_y())
        else:
            # Preserve the last non-maximized size when closing a zoomed window.
            previous.update(self._last_normal_main)
        previous['maximized']=(state=='zoomed')
        self.ui_layout['main']=previous
        if self.main_split.winfo_width()>550:
            self.ui_layout['sidebar_width']=self.main_split.sash_coord(0)[0]
        if self.notes_split.winfo_height()>130:
            self.ui_layout['notes_fraction']=round(
                self.notes_split.sash_coord(0)[1]/self.notes_split.winfo_height(),4)

    def _save_layout(self):
        self._capture_layout()
        save_layout(self.ui_layout)

    def _dialog_geometry(self,win,key,default,minimum):
        screen=(win.winfo_screenwidth(),win.winfo_screenheight())
        win.geometry(dialog_geometry(self.ui_layout,key,default,minimum,screen))
        def remember(event):
            if event.widget is win and win.state()=='normal':
                self.ui_layout.setdefault('dialogs',{})[key]=[
                    win.winfo_width(),win.winfo_height()]
        win.bind('<Configure>',remember,add='+')
        def on_destroy(event):
            if event.widget is win:
                try:
                    self._save_layout()
                except OSError:
                    # Dialog closure must never risk customer data.
                    pass
        win.bind('<Destroy>',on_destroy,add='+')

    def _build_ui(self):
        top=tk.Frame(self.root,bg=COLORS['white'],height=44)
        top.pack(fill='x')
        top.pack_propagate(False)
        brand=tk.Frame(top,bg=COLORS['white'])
        brand.pack(side='left',padx=11,pady=8)
        label(brand,'▣',size=13,fg=COLORS['blue']).pack(side='left',padx=(0,6))
        label(brand,'Order Notes',size=12,bold=True).pack(side='left')
        self.connection=tk.StringVar(value='●  Gateway not checked')
        more=tk.Menubutton(top,text='More ▾',relief='flat',bd=0,fg=COLORS['ink'],
                           bg=COLORS['white'],activebackground=COLORS['gray_light'],
                           font=('Segoe UI',9),padx=9,pady=3,cursor='hand2')
        menu=tk.Menu(more,tearoff=False)
        menu.add_command(label='SMS templates',command=self.edit_templates)
        menu.add_command(label='Export / Backup',command=self.export_menu)
        menu.add_command(label='Help',command=self.show_help)
        more.configure(menu=menu)
        more.pack(side='right',padx=(0,10))
        button(top,'⚙ Settings',self.open_settings,small=True).pack(side='right',padx=2)
        self.connection_label=label(top,textvariable=self.connection,size=8,fg=COLORS['muted'])
        self.connection_label.pack(side='right',padx=(3,8))
        tk.Frame(self.root,height=1,bg=COLORS['line']).pack(fill='x')

        self.stats=tk.Frame(self.root,bg=COLORS['bg'])
        self.stats.pack(fill='x',padx=8,pady=(6,5))
        self.stats_labels={}
        for key,title in [('to_notify','To notify'),('awaiting_pickup','Awaiting pickup'),('completed','Collected'),('all','Total requests')]:
            card=tk.Frame(self.stats,bg=COLORS['white'],highlightbackground=COLORS['line'],highlightthickness=1)
            card.pack(side='left',fill='x',expand=True,padx=(0,5))
            num=label(card,'0',size=13,bold=True)
            num.pack(anchor='w',padx=9,pady=(3,0))
            label(card,title,size=8,fg=COLORS['muted']).pack(anchor='w',padx=9,pady=(0,4))
            self.stats_labels[key]=num
            for child in (card,num):
                child.bind('<Button-1>',lambda e,k=key:self.set_filter(k))

        content=tk.Frame(self.root,bg=COLORS['bg'])
        content.pack(fill='both',expand=True,padx=8,pady=(0,3))
        # Drag the vertical divider to resize the request list; remember it.
        main_split=tk.PanedWindow(content,orient='horizontal',bg=COLORS['line'],
                                  sashwidth=6,sashrelief='raised',showhandle=False,
                                  bd=0,relief='flat',opaqueresize=True)
        main_split.pack(fill='both',expand=True)
        self.main_split=main_split
        main_split.bind('<ButtonPress-1>',lambda e:setattr(self,'_split_touched',True),add='+')
        left=tk.Frame(main_split,bg=COLORS['white'],width=264,
                      highlightbackground=COLORS['line'],highlightthickness=1)
        self.sidebar=left
        left.pack_propagate(False)
        main_split.add(left,minsize=210,stretch='never')
        toolbar=tk.Frame(left,bg=COLORS['white'])
        toolbar.pack(fill='x',padx=8,pady=(5,4))
        label(toolbar,'Requests',size=10,bold=True).pack(side='left')
        button(toolbar,'+  New request',self.new_request,primary=True,small=True).pack(side='right')
        self.search=tk.StringVar()
        self.search.trace_add('write',lambda *_:self.queue_refresh())
        se=entry(left,self.search)
        self.search_entry=se
        se.pack(fill='x',padx=8,pady=(0,4),ipady=1)
        se.insert(0,'')
        self.filter_bar=tk.Frame(left,bg=COLORS['white'])
        self.filter_bar.pack(fill='x',padx=6,pady=(0,5))
        self.filter_buttons={}
        for key,title in [('to_notify','To notify'),('awaiting_pickup','Pickup'),('completed','Done'),('all','All')]:
            b=button(self.filter_bar,title,lambda k=key:self.set_filter(k),small=True)
            b.pack(side='left',padx=2)
            self.filter_buttons[key]=b
        tk.Frame(left,height=1,bg=COLORS['line']).pack(fill='x')
        self.list_nav=tk.Frame(left,bg=COLORS['white'])
        self.list_nav.pack(side='bottom',fill='x',padx=7,pady=(3,6))
        self.previous_list=button(self.list_nav,'‹  Prev',lambda:self.change_list_page(-1),small=True)
        self.previous_list.pack(side='left')
        self.list_page_label=label(self.list_nav,'Page 1 / 1',size=8,fg=COLORS['muted'])
        self.list_page_label.pack(side='left',expand=True)
        self.next_list=button(self.list_nav,'Next  ›',lambda:self.change_list_page(1),small=True)
        self.next_list.pack(side='right')
        self.list_inner=tk.Frame(left,bg=COLORS['white'])
        self.list_inner.pack(fill='both',expand=True)
        self.list_inner.bind('<Configure>',self._list_region_changed)
        self.request_menu=tk.Menu(self.root,tearoff=False,activebackground=COLORS['blue_light'],
                                  activeforeground=COLORS['ink'])

        right=tk.Frame(main_split,bg=COLORS['white'],highlightbackground=COLORS['line'],highlightthickness=1)
        main_split.add(right,minsize=330,stretch='always')
        self._build_right(right)
        location='shared folder · multiple computers' if is_network_folder(HOME) else 'portable data folder'
        self.banner=tk.StringVar(value='Ready · '+location)
        label(self.root,textvariable=self.banner,bg=COLORS['bg'],fg=COLORS['muted'],size=8).pack(fill='x',anchor='w',padx=10,pady=(0,2))
        self._set_filter_look()

    def _build_right(self,right):
        head=tk.Frame(right,bg=COLORS['white'])
        head.pack(fill='x',padx=9,pady=(5,2))
        self.request_heading=label(head,'New request',size=11,bold=True)
        self.request_heading.pack(side='left')
        self.badge=label(head,'DRAFT',size=8,bold=True,fg=COLORS['blue'],bg=COLORS['blue_light'],padx=5,pady=2)
        self.badge.pack(side='left',padx=6)
        self.change_status_button=button(head,'Change status',self.change_status,small=True)
        self.change_status_button.pack(side='right',padx=(4,0))
        self.delete_button=button(head,'Delete',self.delete_selected_request,small=True,danger=True)
        self.delete_button.pack(side='right',padx=(3,0))
        self.delete_button.pack_forget()
        self.quick_actions=tk.Frame(right,bg=COLORS['white'])
        self.quick_actions.pack(fill='x',padx=9,pady=(0,2))

        self.nb=ttk.Notebook(right,style='Modern.TNotebook')
        editor=tk.Frame(self.nb,bg=COLORS['white'])
        sms_tab=tk.Frame(self.nb,bg=COLORS['white'])
        hist=tk.Frame(self.nb,bg=COLORS['white'])
        self.nb.add(editor,text='  Details  ')
        self.nb.add(sms_tab,text='  SMS  ')
        self.nb.add(hist,text='  Activity  ')
        self._build_editor(editor)
        self._build_sms_tab(sms_tab)
        activity_nav=tk.Frame(hist,bg=COLORS['white'])
        activity_nav.pack(side='bottom',fill='x',padx=8,pady=5)
        self.previous_activity=button(activity_nav,'‹  Previous',lambda:self.change_activity_page(-1),small=True)
        self.previous_activity.pack(side='left')
        self.activity_page_label=label(activity_nav,'',size=8,fg=COLORS['muted'])
        self.activity_page_label.pack(side='left',expand=True)
        self.next_activity=button(activity_nav,'Next  ›',lambda:self.change_activity_page(1),small=True)
        self.next_activity.pack(side='right')
        self.history_box=tk.Text(hist,wrap='word',state='disabled',font=('Segoe UI',9),bg=COLORS['white'],
                                 fg=COLORS['ink'],relief='flat',bd=0,padx=9,pady=6)
        self.history_box.pack(fill='both',expand=True)
        self.history_box.bind('<MouseWheel>',lambda e:'break')
        self.history_box.bind('<Button-4>',lambda e:'break')
        self.history_box.bind('<Button-5>',lambda e:'break')
        self.history_box.tag_configure('date',foreground=COLORS['muted'],font=('Segoe UI',8))
        self.history_box.tag_configure('heading',foreground=COLORS['blue'],font=('Segoe UI',9,'bold'))
        self.history_box.tag_configure('body',spacing3=9)

        foot=tk.Frame(right,bg=COLORS['white'])
        foot.pack(side='bottom',fill='x',padx=9,pady=4)
        self.save_label=label(foot,'Draft · not yet saved',size=8,fg=COLORS['muted'])
        self.save_label.pack(side='left')
        self.save_button=button(foot,'Save new request',self.save_new,primary=True,small=True)
        self.save_button.pack(side='right')
        # The footer remains visible even if the window is very short.
        self.send_btn=button(foot,'Send SMS →',self.send_sms,primary=True,small=True)
        self.nb.pack(fill='both',expand=True,padx=6,pady=(0,2))

    def _build_editor(self,parent):
        # Fixed-height form: no Canvas, Scrollbar or vertical document scrolling.
        inner=tk.Frame(parent,bg=COLORS['white'])
        inner.pack(fill='both',expand=True)
        self.editor_inner=inner
        fields=tk.Frame(inner,bg=COLORS['white'])
        fields.pack(fill='x',padx=8,pady=(4,1))
        fields.grid_columnconfigure(0,weight=1)
        fields.grid_columnconfigure(1,weight=1)
        label(fields,'PHONE NUMBER  *',size=8,bold=True,fg=COLORS['muted']).grid(row=0,column=0,sticky='w')
        label(fields,'CUSTOMER NAME  ·  optional',size=8,bold=True,fg=COLORS['muted']).grid(row=0,column=1,sticky='w',padx=(10,0))
        self.phone_var=tk.StringVar()
        self.name_var=tk.StringVar()
        phone_container=tk.Frame(fields,bg=COLORS['white'])
        phone_container.grid(row=1,column=0,sticky='ew',pady=(2,5))
        self.phone_entry=entry(phone_container,self.phone_var)
        self.phone_entry.pack(side='left',fill='x',expand=True,ipady=2)
        button(phone_container,'Copy',self.copy_phone,small=True).pack(side='right',padx=(4,0))
        self.name_entry=entry(fields,self.name_var)
        self.name_entry.grid(row=1,column=1,sticky='ew',padx=(10,0),pady=(2,5),ipady=2)
        # Draggable splitter lets users resize Private Notes without scrollbars.
        split=tk.PanedWindow(inner,orient='vertical',bg=COLORS['line'],
                             sashwidth=7,sashrelief='raised',showhandle=True,
                             handlesize=12,handlepad=8,bd=0,relief='flat',opaqueresize=True)
        split.pack(fill='both',expand=True,padx=8,pady=(0,2))
        self.notes_split=split
        split.bind('<ButtonPress-1>',lambda e:setattr(self,'_split_touched',True),add='+')
        items_pane=tk.Frame(split,bg=COLORS['white'])
        label(items_pane,'ITEMS / PRODUCTS  ·  optional',size=8,bold=True,
              fg=COLORS['muted']).pack(anchor='w')
        self.items_box=text_box(items_pane,height=3)
        self.items_box.pack(fill='both',expand=True,pady=(2,2))
        notes_pane=tk.Frame(split,bg=COLORS['white'])
        label(notes_pane,'PRIVATE NOTES  ·  drag divider above to resize',size=8,bold=True,
              fg=COLORS['muted']).pack(anchor='w')
        self.notes_box=text_box(notes_pane,height=3)
        self.notes_box.pack(fill='both',expand=True,pady=(2,2))
        # At a compact 800x520 window the notes splitter can be only ~190px
        # high. A 65px pane minimum makes a 65/35 split mathematically
        # impossible (Tk clamps the sash to 115/190). Keep both editors
        # readable while allowing saved ratios to be restored on Windows.
        split.add(items_pane,minsize=50,stretch='always')
        split.add(notes_pane,minsize=50,stretch='always')
        for var in (self.phone_var,self.name_var):
            var.trace_add('write',lambda *_:self.schedule_save())
        for widget in (self.items_box,self.notes_box):
            widget.bind('<KeyRelease>',lambda e:self.schedule_save())
            widget.bind('<<Paste>>',lambda e:self._after(100,self.schedule_save))

        self.meta_label=label(inner,'',size=8,fg=COLORS['muted'],justify='left')
        self.meta_label.pack(anchor='w',padx=8,pady=(1,4))

    def _build_sms_tab(self,parent):
        sms_section=tk.Frame(parent,bg=COLORS['white'])
        sms_section.pack(fill='both',expand=True,padx=8,pady=(6,4))
        choose=tk.Frame(sms_section,bg=COLORS['white'])
        choose.pack(fill='x')
        choose.grid_columnconfigure(0,weight=1)
        label(choose,'SMS TEMPLATE',size=8,bold=True,fg=COLORS['muted']).grid(row=0,column=0,sticky='w')
        label(choose,'SEND VIA',size=8,bold=True,fg=COLORS['muted']).grid(row=0,column=1,sticky='w',padx=(8,0))
        self.template_var=tk.StringVar()
        self.template_combo=ttk.Combobox(choose,state='readonly',textvariable=self.template_var,style='Modern.TCombobox')
        self.template_combo.grid(row=1,column=0,sticky='ew',pady=(2,4))
        self.template_combo.bind('<<ComboboxSelected>>',lambda e:self._compose_sms())
        self.sim_var=tk.StringVar(value='SIM '+self.settings.get('default_sim','1'))
        sim_combo=ttk.Combobox(choose,state='readonly',textvariable=self.sim_var,values=['SIM 1','SIM 2'],width=7,style='Modern.TCombobox')
        sim_combo.grid(row=1,column=1,sticky='e',padx=(8,0),pady=(2,4))
        self.with_items=tk.BooleanVar(value=True)
        tk.Checkbutton(sms_section,text='Include item list in SMS',variable=self.with_items,command=self._compose_sms,
                       bg=COLORS['white'],fg=COLORS['ink'],selectcolor=COLORS['white'],
                       activebackground=COLORS['white'],font=('Segoe UI',9)).pack(anchor='w',pady=(0,3))
        self.sms_box=text_box(sms_section,height=4)
        self.sms_box.pack(fill='both',expand=True)
        actions=tk.Frame(sms_section,bg=COLORS['white'])
        actions.pack(fill='x',pady=(4,1))
        self.sms_count=label(actions,'',size=8,fg=COLORS['muted'])
        self.sms_count.pack(side='left')
        self.sms_box.bind('<KeyRelease>',lambda e:self._sms_count())

    def _sms_count(self):
        s=text_get(self.sms_box)
        encoding,units,parts=estimate_sms(s)
        self.sms_count.configure(text=f'{len(s)} chars · {parts} SMS part(s) · {encoding}')

    def _compose_sms(self):
        tpl=self.get_selected_template()
        base=tpl['body'] if tpl else ''
        items=text_get(self.items_box).strip()
        if '{items}' in base:
            base=base.replace('{items}',items if self.with_items.get() else '')
        elif items and self.with_items.get():
            base+='\n\nBeställda varor:\n'+items
        set_text(self.sms_box,base)
        self._sms_count()

    def get_selected_template(self):
        try:
            return self.templates[self.template_combo.current()]
        except (IndexError,KeyError):
            return None

    def refresh_templates(self):
        old=self.get_selected_template()
        old_id=old['id'] if old else None
        self.templates=self.db.templates()
        self.template_names=[t['title'] for t in self.templates]
        self.template_combo['values']=self.template_names
        chosen=next((i for i,t in enumerate(self.templates) if t['id']==old_id),0)
        if self.templates:
            self.template_combo.current(chosen)
            self._compose_sms()

    def _fields(self):
        return {'phone':self.phone_var.get().strip(),'name':self.name_var.get().strip(),
                'items':text_get(self.items_box),'notes':text_get(self.notes_box)}

    def schedule_save(self):
        if self.loading:
            return
        if self.autosave_after:
            self._cancel_after(self.autosave_after)
        self.save_label.configure(text='Editing…',fg=COLORS['muted'])
        self.autosave_after=self._after(700,self._autosave)

    def _autosave(self):
        self.autosave_after=None
        if self.loading:
            return
        if self.edit_conflict:
            self.save_label.configure(text='Not saved · reload latest record',fg=COLORS['red'])
            return
        vals=self._fields()
        if self.selected_id is None:
            try:
                save_json_atomic(draft_file(HOME),vals)
                self.save_label.configure(text='Draft safely stored',fg=COLORS['muted'])
            except OSError:
                self.save_label.configure(text='Draft backup failed',fg=COLORS['red'])
            return
        try:
            self.selected_revision=self.db.update_request(
                self.selected_id,vals['phone'],vals['name'],vals['items'],vals['notes'],
                self.settings.get('country_code','46'),expected_revision=self.selected_revision)
            self.save_label.configure(text='✓  All changes saved',fg=COLORS['green'])
            self.queue_refresh()
        except ConcurrentUpdateError:
            self._handle_conflict()
        except ValueError:
            self.save_label.configure(text='Enter a valid phone number to save',fg=COLORS['red'])
        except Exception as exc:
            self.save_label.configure(text='Save failed: '+str(exc),fg=COLORS['red'])

    def _flush(self):
        if self.autosave_after:
            self._cancel_after(self.autosave_after)
            self._autosave()

    def save_new(self):
        if self.selected_id is not None:
            self._flush()
            return True
        self._flush()
        vals=self._fields()
        if not valid_phone(vals['phone'],self.settings.get('country_code','46')):
            messagebox.showwarning('Phone number required','Enter a valid phone number before saving the request.',parent=self.root)
            self.phone_entry.focus_set()
            return False
        dup=self.db.duplicate_phone(normalized_phone(vals['phone'],self.settings.get('country_code','46')))
        if dup and not messagebox.askyesno('Existing customer request',
                    f'This phone number already has {len(dup)} active request(s).\n\nCreate another request anyway?',parent=self.root):
            return False
        try:
            new_id=self.db.new_request(vals['phone'],vals['name'],vals['items'],vals['notes'],self.settings.get('country_code','46'))
            try:
                draft_file(HOME).unlink(missing_ok=True)
            except OSError:
                pass
            # The new record is permanent; present an empty draft for the next customer.
            # Existing records still stay editable by selecting them from the left list.
            self.new_request()
            self.banner.set(f'Request #{new_id} saved · ready for the next customer')
            return True
        except Exception as exc:
            messagebox.showerror('Cannot save request',str(exc),parent=self.root)
            return False

    def new_request(self):
        self._flush()
        self.selected_id=None
        self.selected_revision=None
        self.edit_conflict=False
        self.loading=True
        draft=load_draft()
        self.phone_var.set(draft.get('phone',''))
        self.name_var.set(draft.get('name',''))
        set_text(self.items_box,draft.get('items',''))
        set_text(self.notes_box,draft.get('notes',''))
        self.loading=False
        self.request_heading.configure(text='New request')
        self.badge.configure(text='DRAFT',bg=COLORS['blue_light'],fg=COLORS['blue'])
        self.send_btn.pack_forget()
        self.delete_button.pack_forget()
        self.save_button.pack(side='right')
        self.save_label.configure(text='Draft · phone number required',fg=COLORS['muted'])
        self.meta_label.configure(text='The request will receive a registration date when saved.')
        self._render_actions()
        self._render_history()
        # A new draft starts completely blank, including its optional SMS composer.
        # Template content is generated once an existing request is opened or
        # the user actively selects a template.
        set_text(self.sms_box,'')
        self._sms_count()
        self.nb.select(0)
        self._after(40,self.phone_entry.focus_set)
        self.refresh_list()

    def show_request(self, req_id):
        if req_id==self.selected_id:
            return
        self._flush()
        req=self.db.get_request(req_id)
        if req:
            self._load_request(req,select_details=True,compose_sms=True)

    def _load_request(self,req,select_details=False,compose_sms=False):
        self.selected_id=req['id']
        self.selected_revision=req['revision']
        self.edit_conflict=False
        self.loading=True
        self.phone_var.set(req['phone'])
        self.name_var.set(req['customer_name'])
        set_text(self.items_box,req['items'])
        set_text(self.notes_box,req['notes'])
        self.loading=False
        self.request_heading.configure(text=f"Request #{req['id']}")
        self._update_badge(req['stage'])
        self.meta_label.configure(text=f"Created: {fmt_timestamp(req['created_at'])}     ·     Updated: {fmt_timestamp(req['updated_at'])}")
        self.save_button.pack_forget()
        self.delete_button.pack(side='right',padx=(3,0),before=self.change_status_button)
        self.send_btn.pack(side='right')
        self.save_label.configure(text='✓  Autosave enabled',fg=COLORS['green'])
        self._render_actions()
        self._render_history()
        if compose_sms:
            self._compose_sms()
        if select_details:
            self.nb.select(0)
        self.refresh_list()

    def reload_current(self,confirm=True):
        if self.selected_id is None:
            return
        if confirm and (self.autosave_after or self.edit_conflict) and not messagebox.askyesno(
                'Reload latest request','Discard unsaved changes and reload the latest version?',parent=self.root):
            return
        if self.autosave_after:
            self._cancel_after(self.autosave_after)
            self.autosave_after=None
        req=self.db.get_request(self.selected_id)
        if req:
            self._load_request(req,compose_sms=False)
            self.banner.set('Latest request loaded')
        else:
            self.new_request()
            self.banner.set('Request was deleted on another computer')

    def _handle_conflict(self):
        if self.edit_conflict:
            return
        self.edit_conflict=True
        self.save_label.configure(text='Not saved · changed on another PC',fg=COLORS['red'])
        if messagebox.askyesno('Request changed elsewhere',
                'This request was changed or deleted on another computer.\n\n'
                'Your edits were NOT saved, so the other changes are safe.\n\n'
                'Reload the latest record? Your unsaved text will be discarded.\n'
                'Choose No to keep the text visible so you can copy it first.',parent=self.root):
            self.reload_current(confirm=False)

    def _shared_refresh(self):
        """Refresh other PCs' changes without overwriting a locally edited form."""
        try:
            if self.selected_id is not None and not self.edit_conflict:
                req=self.db.get_request(self.selected_id)
                if req is None:
                    if not self.autosave_after:
                        self.new_request()
                        self.banner.set('Selected request was deleted on another computer')
                elif req['revision']!=self.selected_revision:
                    if not self.autosave_after:
                        self._load_request(req,compose_sms=False)
                        self.banner.set('Request updated from shared database')
            self.refresh_list()
        except (sqlite3.Error,OSError) as exc:
            self.banner.set('Shared database unavailable: '+str(exc)[:110])
        finally:
            self._after(5000,self._shared_refresh)

    def _update_badge(self,stage):
        bg,fg=BADGES.get(stage,(COLORS['gray_light'],COLORS['muted']))
        self.badge.configure(text=STAGE_LABELS.get(stage,stage).upper(),bg=bg,fg=fg)

    def _render_actions(self):
        for w in self.quick_actions.winfo_children():
            w.destroy()
        if not self.selected_id:
            label(self.quick_actions,'Record a request first to manage its order status.',size=9,fg=COLORS['muted']).pack(side='left')
            return
        req=self.db.get_request(self.selected_id)
        opts=[]
        if req['stage']=='new':
            opts.append(('✓  Mark ordered','ordered'))
        if req['stage'] in ('new','ordered'):
            opts.append(('✓  Mark arrived','arrived'))
        if req['stage']!='collected':
            opts.append(('✓  Mark collected','collected'))
        for title,stage in opts:
            button(self.quick_actions,title,lambda s=stage:self.mark_stage(s),small=True,
                   tone=stage).pack(side='left',padx=(0,7))

    def delete_selected_request(self):
        req_id=self.selected_id
        if req_id is None:
            return
        self._flush()
        current=self.db.get_request(req_id)
        if current is None:
            return
        number=current['phone']
        if not messagebox.askyesno('Delete request',
                f'Permanently delete Request #{req_id} ({number})?\n\n'
                'Its notes, SMS history and reminders will also be deleted.\n'
                'This cannot be undone.\n\nDelete this request?',
                icon='warning',parent=self.root):
            return
        try:
            if self.db.delete_request(req_id,expected_revision=current['revision']):
                self.selected_id=None
                # A deleted record must not be re-saved by a pending autosave.
                if self.autosave_after:
                    self._cancel_after(self.autosave_after)
                    self.autosave_after=None
                draft_file(HOME).unlink(missing_ok=True)
                self.new_request()
                self.banner.set(f'Request #{req_id} permanently deleted')
            else:
                messagebox.showwarning('Not found','The request no longer exists.',parent=self.root)
        except ConcurrentUpdateError:
            self._handle_conflict()
        except Exception as exc:
            messagebox.showerror('Delete failed',str(exc),parent=self.root)

    def mark_stage(self,stage):
        if not self.selected_id:
            return
        self._flush()
        if stage=='collected' and not messagebox.askyesno('Confirm pickup','Mark this request as collected by the customer?',parent=self.root):
            return
        try:
            self.db.set_stage(self.selected_id,stage,'Quick action',expected_revision=self.selected_revision)
            self._refresh_current()
        except ConcurrentUpdateError:
            self._handle_conflict()

    def change_status(self):
        if not self.selected_id:
            messagebox.showinfo('Save first','Save this request before changing its status.',parent=self.root)
            return
        self._flush()
        req=self.db.get_request(self.selected_id)
        win=tk.Toplevel(self.root)
        win.title('Change request status')
        win.geometry('380x205')
        win.resizable(False,False)
        win.configure(bg=COLORS['white'])
        win.transient(self.root)
        win.grab_set()
        label(win,'Correct request status',size=15,bold=True).pack(anchor='w',padx=22,pady=(22,7))
        label(win,'All manual changes are recorded in the activity history.',fg=COLORS['muted'],size=9).pack(anchor='w',padx=22)
        var=tk.StringVar(value=STAGE_LABELS[req['stage']])
        opts=list(STAGE_LABELS.values())
        cb=ttk.Combobox(win,state='readonly',values=opts,textvariable=var,style='Modern.TCombobox')
        cb.pack(fill='x',padx=22,pady=20)
        def commit():
            new=next(k for k,v in STAGE_LABELS.items() if v==var.get())
            if new!=req['stage']:
                try:
                    self.db.set_stage(self.selected_id,new,'Corrected by user',expected_revision=req['revision'])
                    self._refresh_current()
                except ConcurrentUpdateError:
                    self._handle_conflict()
            win.destroy()
        button(win,'Apply status',commit,primary=True).pack(side='right',padx=22,pady=(0,14))

    def _refresh_current(self):
        if not self.selected_id:
            return
        req=self.db.get_request(self.selected_id)
        if not req:
            return
        if req['revision']!=self.selected_revision:
            if self.autosave_after or self.edit_conflict:
                # Do not adopt the new revision while an older form is being
                # edited: doing so would allow a later autosave to overwrite it.
                self.banner.set('This request changed elsewhere · save or reload carefully')
                return
            self._load_request(req,compose_sms=False)
            return
        self._update_badge(req['stage'])
        self._render_actions()
        self._render_history()
        self.meta_label.configure(text=f"Created: {fmt_timestamp(req['created_at'])}     ·     Updated: {fmt_timestamp(req['updated_at'])}")
        self.refresh_list()

    def _render_history(self):
        import textwrap
        pages=[]
        if self.selected_id is None:
            pages=[('','', 'Save a request to start its activity timeline.')]
        else:
            events,sms=self.db.activity(self.selected_id)
            timeline=[]
            for item in events:
                timeline.append((item['occurred_at'],item['event_type'],item['detail'] or '—'))
            for item in sms:
                timeline.append((item['created_at'],
                    f"SMS {item['purpose']} · {item['status'].upper()} · SIM {item['sim']} · {item['recipient']}",
                    item['message']))
            timeline.sort(key=lambda it:it[0],reverse=True)
            for stamp,title,body in timeline:
                # Split very long SMS bodies into separate pages, never hidden by a scrollbar.
                lines=[]
                for line in str(body).splitlines():
                    lines+=textwrap.wrap(line,width=55,break_long_words=True) or ['']
                for i in range(0,max(1,len(lines)),10):
                    suffix=f' (part {i//10+1})' if len(lines)>10 else ''
                    pages.append((stamp,title+suffix,'\n'.join(lines[i:i+10])))
        self.activity_pages=pages or [('','','No activity recorded.')]
        self.activity_page=0
        self._show_activity_page()

    def change_activity_page(self,delta):
        self.activity_page=max(0,min(self.activity_page+delta,len(self.activity_pages)-1))
        self._show_activity_page()

    def _show_activity_page(self):
        self.history_box.configure(state='normal')
        self.history_box.delete('1.0','end')
        stamp,title,body=self.activity_pages[self.activity_page]
        if stamp:
            self.history_box.insert('end',fmt_timestamp(stamp)+'\n\n','date')
        if title:
            self.history_box.insert('end',title+'\n\n','heading')
        self.history_box.insert('end',body,'body')
        self.history_box.configure(state='disabled')
        self.activity_page_label.configure(text=f'{self.activity_page+1} / {len(self.activity_pages)}')
        self.previous_activity.configure(state='normal' if self.activity_page>0 else 'disabled')
        self.next_activity.configure(state='normal' if self.activity_page<len(self.activity_pages)-1 else 'disabled')

    def send_sms(self):
        if self.sending:
            return
        if not self.selected_id:
            messagebox.showwarning('Save request','Save the request before sending a text message.',parent=self.root)
            return
        self._flush()
        req=self.db.get_request(self.selected_id)
        message=text_get(self.sms_box).strip()
        selected=self.get_selected_template()
        kind=selected['kind'] if selected else 'other'
        sim=2 if self.sim_var.get()=='SIM 2' else 1
        if not message:
            messagebox.showwarning('Empty SMS','Write an SMS message before sending.',parent=self.root)
            return
        if not valid_phone(req['phone'],self.settings.get('country_code','46')):
            messagebox.showwarning('Invalid phone','Check the customer phone number.',parent=self.root)
            return
        if not messagebox.askyesno('Confirm SMS send',
                 f'Send the following SMS to {req["phone"]} using SIM {sim}?\n\n{message[:700]}'+
                 ('…' if len(message)>700 else ''),parent=self.root):
            return
        conf=self.db.settings()
        passwd=self.gateway_password
        req_id=self.selected_id
        dest=req['phone']
        try:
            gateway=Gateway(conf.get('gateway_url',''),conf.get('gateway_user',''),passwd,conf.get('country_code','46'))
            log_id=self.db.begin_sms(req_id,dest,sim,message,kind)
        except (GatewayError,Exception) as exc:
            messagebox.showerror('Cannot send SMS',str(exc),parent=self.root)
            return
        self.sending=True
        self.send_btn.configure(state='disabled',text='Sending…')
        def worker():
            try:
                response=gateway.send(dest,message,sim)
                self.db.complete_sms(log_id,'queued',str(response)[:500])
                error_text=None
            except GatewayError as exc:
                status='uncertain' if exc.uncertain else 'failed'
                self.db.complete_sms(log_id,status,str(exc))
                error_text=str(exc)
            except Exception as exc:
                self.db.complete_sms(log_id,'uncertain',str(exc))
                error_text='Unknown error. Check the gateway before trying again.'
            self._post_ui(lambda:self._on_sms_result(req_id,error_text))
        threading.Thread(target=worker,daemon=True,name='Customer-SMS').start()

    def _on_sms_result(self,req_id,error_text):
        self.sending=False
        self.send_btn.configure(state='normal',text='Send SMS  →')
        if error_text:
            messagebox.showwarning('SMS not confirmed',error_text+'\n\nCheck SMSGate before retrying; the message might already be queued.',parent=self.root)
            self.banner.set('SMS result uncertain or failed · review Activity history')
        else:
            self.banner.set('SMS accepted by gateway · delivery is not yet guaranteed')
            messagebox.showinfo('SMS accepted','The gateway has accepted the SMS for sending. Delivery to the recipient is not yet confirmed.',parent=self.root)
        if req_id==self.selected_id:
            self._refresh_current()
        else:
            self.refresh_list()

    def set_filter(self,name):
        self.list_page=0
        self.filter=name
        self._set_filter_look()
        self.refresh_list()

    def _set_filter_look(self):
        for k,b in self.filter_buttons.items():
            current=k==self.filter
            if current:
                set_button_colors(b,COLORS['blue'],COLORS['white'],COLORS['blue_hover'])
            else:
                set_button_colors(b,COLORS['gray_light'],COLORS['ink'],COLORS['neutral_hover'])

    def _list_region_changed(self,event):
        size=(event.width,event.height)
        if size==self._list_size or size[0]<50 or size[1]<50:
            return
        self._list_size=size
        if not self._layout_refresh_queued:
            self._layout_refresh_queued=True
            self._after_idle(self._complete_layout_refresh)

    def _complete_layout_refresh(self):
        self._layout_refresh_queued=False
        self.refresh_list()

    def change_list_page(self,delta):
        self.list_page=max(0,self.list_page+delta)
        self.refresh_list()

    def queue_refresh(self):
        if self.refresh_after:
            self._cancel_after(self.refresh_after)
        self.refresh_after=self._after(180,self.refresh_list)

    def refresh_list(self):
        self.refresh_after=None
        requests=self.db.requests(self.search.get(),self.filter)
        counts=self.db.counts()
        for k,widget in self.stats_labels.items():
            widget.configure(text=str(counts[k]))
        # Physical list paging instead of an on-screen scroll bar.
        # Leave room for separators, fonts and a short status label.
        panel_h=max(90,self.list_inner.winfo_height())
        page_size=max(1,(panel_h-8)//79)
        max_page=max(0,(len(requests)-1)//page_size)
        self.list_page=min(max(0,self.list_page),max_page)
        pages=max_page+1
        self.list_page_label.configure(text=f'{self.list_page+1} / {pages}  ·  {len(requests)}')
        self.previous_list.configure(state='normal' if self.list_page>0 else 'disabled')
        self.next_list.configure(state='normal' if self.list_page<max_page else 'disabled')
        for child in self.list_inner.winfo_children():
            child.destroy()
        if not requests:
            label(self.list_inner,'No matching requests',size=9,fg=COLORS['muted']).pack(pady=24)
            return
        for req in requests[self.list_page*page_size:(self.list_page+1)*page_size]:
            chosen=req['id']==self.selected_id
            bg=COLORS['blue_light'] if chosen else COLORS['white']
            frame=tk.Frame(self.list_inner,bg=bg,padx=7,pady=3,cursor='hand2',
                           highlightbackground=COLORS['blue'] if chosen else COLORS['line'],
                           highlightthickness=1 if chosen else 0)
            frame.pack(fill='x',padx=3,pady=1)
            items=req['items'].strip().splitlines()
            raw_title=items[0] if items else (req['customer_name'] or 'No item description')
            max_px=max(70,self.sidebar.winfo_width()-25)
            display_font=tkfont.Font(family='Segoe UI',size=9,weight='bold')
            title=raw_title
            if display_font.measure(title)>max_px:
                while len(title)>2 and display_font.measure(title+'…')>max_px:
                    title=title[:-1]
                title=title.rstrip()+'…'
            item_label=label(frame,title,size=9,bold=True,bg=bg,anchor='w')
            item_label.pack(anchor='w',fill='x')
            phone=label(frame,req['phone'],size=13,bold=True,bg=bg,anchor='w',cursor='hand2')
            phone.pack(fill='x',pady=(1,0))
            bottom=tk.Frame(frame,bg=bg)
            bottom.pack(fill='x')
            stamp=fmt_timestamp(req['created_at']).split(' · ')[0]
            name=(req['customer_name']+' · ') if req['customer_name'] else ''
            subtitle=label(bottom,name+stamp,size=8,fg=COLORS['muted'],bg=bg,anchor='w')
            subtitle.pack(side='left')
            badge_bg,badge_fg=BADGES[req['stage']]
            badge=label(bottom,STAGE_LABELS[req['stage']],size=8,bold=True,fg=badge_fg,bg=badge_bg,padx=3)
            badge.pack(side='right')
            for w in (frame,item_label,bottom,badge,subtitle):
                w.bind('<Button-1>',lambda e,rid=req['id']:self.show_request(rid))
            phone.bind('<Button-1>',lambda e,number=req['phone']:self.copy_value(number))
            for w in (frame,item_label,phone,bottom,badge,subtitle):
                w.bind('<Button-3>',lambda e,rid=req['id']:self.show_request_menu(e,rid))

    def show_request_menu(self,event,req_id):
        req=self.db.get_request(req_id)
        if not req:
            self.refresh_list()
            return 'break'
        # Select the row so menu actions target the right customer.
        self.show_request(req_id)
        menu=self.request_menu
        menu.delete(0,'end')
        menu.add_command(label='Open request',command=lambda:self.show_request(req_id))
        menu.add_command(label='Reload latest',command=self.reload_current)
        menu.add_separator()
        menu.add_command(label='Copy phone number',command=lambda:self.copy_value(req['phone']))
        menu.add_command(label='Send SMS...',command=lambda:self.nb.select(1))
        menu.add_separator()
        for title,stage in [('Mark ordered','ordered'),('Mark arrived','arrived'),('Mark collected','collected')]:
            menu.add_command(label=title,command=lambda s=stage:self.mark_stage(s),
                             state='disabled' if req['stage']==stage or req['stage']=='collected' else 'normal')
        menu.add_command(label='Change status...',command=self.change_status)
        menu.add_separator()
        menu.add_command(label='Delete request...',command=self.delete_selected_request)
        try:
            menu.tk_popup(event.x_root,event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def copy_value(self,number):
        self.root.clipboard_clear()
        self.root.clipboard_append(number)
        self.banner.set('Phone number copied: '+number)

    def copy_phone(self):
        self.copy_value(self.phone_var.get())

    def _daily_backup(self):
        backup_dir=HOME/'backups'
        backup_dir.mkdir(parents=True,exist_ok=True)
        suffix=backup_suffix(is_network_folder(HOME))
        target=backup_dir/f"order_notes_{date.today():%Y-%m-%d}{suffix}.db"
        if not target.exists():
            try:
                self.db.backup_to(target)
                older=sorted(backup_dir.glob('order_notes_????-??-??'+suffix+'.db'))
                for item in older[:-14]:
                    item.unlink(missing_ok=True)
            except (OSError,sqlite3.Error) as exc:
                self.banner.set('Automatic backup failed: '+str(exc))

    def focus_search(self):
        self.search_entry.focus_set()
        self.search_entry.selection_range(0,'end')

    def show_help(self):
        win=tk.Toplevel(self.root)
        win.title('Order Notes — Quick help')
        self._dialog_geometry(win,'help',
                              (min(580,max(430,win.winfo_screenwidth()-30)),
                               min(420,max(300,win.winfo_screenheight()-90))),
                              (430,300))
        win.configure(bg=COLORS['white'])
        win.transient(self.root)
        win.grab_set()
        label(win,'Order Notes — Quick help',size=18,bold=True).pack(anchor='w',padx=25,pady=(19,12))
        help_text=(
            '1. Create a request\n'
            'Click New request (Ctrl+N). Only a phone number is required. Enter products and private notes if needed, then save. Editing saved requests is automatic.\n\n'
            '2. Track the order\n'
            'Click Mark ordered when you have ordered the item. Click Mark arrived when it reaches the store. Click Mark collected after the customer picks it up. Change status lets you correct mistakes.\n\n'
            '3. Send an SMS\n'
            'Choose a Swedish SMS template. Optionally include the products, edit the text and choose SIM 1 or 2. Confirm before sending. ONLY a template marked Arrival notification changes the request to Customer notified when SMSGate accepts it.\n\n'
            '4. Personal SMS reminders\n'
            'Set your phone number and global reminder delays in Settings. Unordered requests and arrived requests waiting for notification trigger recurring reminders to you. After the PC is powered off, an overdue reminder is sent when the app next opens and the gateway is available.\n\n'
            '5. Search and history\n'
            'Use Ctrl+F, status filters and Activity. Right-click a request for Copy phone, SMS, status changes, Reload latest and Delete. Drag the divider above Private Notes or the divider beside the request list to resize them. The sizes are remembered on this PC.\n\n'
            '6. Backup and privacy\n'
            'Export CSV or back up the database from Export / Backup. Daily backups are kept in the data folder. When sharing via SMB, all PCs must have write access; SQLite network storage depends on reliable SMB file locking. For critical multi-PC use, prefer a central server.\n\n'
            'Important: A gateway timeout can mean the SMS was already accepted. Inspect the SMS history before retrying a message. Order Notes asks before closing and prevents opening a second copy on the same Windows PC.'
        )
        articles=[part for part in help_text.split('\n\n') if part.strip()]
        index=[0]
        pane=label(win,'',size=10,justify='left',anchor='nw',wraplength=450)
        pane.pack(fill='both',expand=True,padx=20,pady=5)
        foot=tk.Frame(win,bg=COLORS['white'])
        foot.pack(fill='x',padx=20,pady=10)
        prev=button(foot,'‹ Previous',lambda:change(-1),small=True)
        prev.pack(side='left')
        page=label(foot,'',size=8,fg=COLORS['muted'])
        page.pack(side='left',expand=True)
        nxt=button(foot,'Next ›',lambda:change(1),small=True)
        nxt.pack(side='left')
        button(foot,'Close',win.destroy,primary=True,small=True).pack(side='right',padx=(9,0))
        def change(delta):
            index[0]=max(0,min(len(articles)-1,index[0]+delta))
            pane.configure(text=articles[index[0]])
            page.configure(text=f'{index[0]+1} / {len(articles)}')
            prev.configure(state='normal' if index[0] else 'disabled')
            nxt.configure(state='normal' if index[0]<len(articles)-1 else 'disabled')
        change(0)

    def export_menu(self):
        win=tk.Toplevel(self.root)
        win.title('Export and backup')
        win.configure(bg=COLORS['white'])
        win.geometry('390x195')
        win.resizable(False,False)
        win.transient(self.root)
        win.grab_set()
        label(win,'Back up your order database',size=14,bold=True).pack(anchor='w',padx=20,pady=(19,7))
        label(win,'Export a spreadsheet or save a complete database copy.',fg=COLORS['muted'],size=9).pack(anchor='w',padx=20)
        bar=tk.Frame(win,bg=COLORS['white'])
        bar.pack(anchor='w',padx=18,pady=23)
        button(bar,'Export CSV',lambda:(win.destroy(),self.export_csv()),primary=True,small=True).pack(side='left',padx=(0,9))
        button(bar,'Backup now',lambda:(win.destroy(),self.backup_now()),small=True).pack(side='left')

    def export_csv(self):
        path=filedialog.asksaveasfilename(parent=self.root,title='Export all requests',
              initialfile='order_notes_export.csv',defaultextension='.csv',filetypes=[('CSV files','*.csv')])
        if not path:
            return
        columns=['id','phone','customer_name','items','notes','stage','created_at','updated_at','ordered_at','arrived_at','notified_at','collected_at']
        try:
            rows=self.db.requests(filter_name='all')
            with open(path,'w',encoding='utf-8-sig',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore')
                writer.writeheader()
                writer.writerows(rows)
            self.banner.set(f'Exported {len(rows)} requests')
        except OSError as exc:
            messagebox.showerror('Export failed',str(exc),parent=self.root)

    def backup_now(self):
        path=filedialog.asksaveasfilename(parent=self.root,title='Back up database',
              initialfile='order_notes_backup.db',defaultextension='.db',filetypes=[('SQLite database','*.db')])
        if path:
            try:
                self.db.backup_to(Path(path))
                self.banner.set('Database backup saved successfully')
            except OSError as exc:
                messagebox.showerror('Backup failed',str(exc),parent=self.root)

    def _post_ui(self, callback):
        """Thread-safe UI delivery; no Tcl/Tk API is called from worker threads."""
        if not self._closing:
            self._ui_events.put(callback)

    def _drain_ui_events(self):
        """Run pending worker completions only on the Tk main thread."""
        if self._closing:
            return
        for _ in range(100):
            try:
                callback=self._ui_events.get_nowait()
            except Empty:
                break
            if self._closing:
                return
            callback()
        if not self._closing:
            self._after(100,self._drain_ui_events)

    def _reminder_tick(self):
        if self._closing:
            return
        if self._reminder_lock.acquire(blocking=False):
            conf=self.db.settings()
            password=self.gateway_password
            def runner():
                try:
                    def update(message):
                        self._post_ui(lambda m=message:self.banner.set(m))
                    process_due_reminders(self.db,conf,password,update)
                except Exception as exc:
                    msg=str(exc)
                    self._post_ui(lambda m=msg:self.banner.set('Reminder check: '+m))
                finally:
                    self._reminder_lock.release()
                    self._post_ui(self._refresh_current)
            self._reminder_thread=threading.Thread(target=runner,daemon=True,name='Reminder-worker')
            self._reminder_thread.start()
        self._after(60000,self._reminder_tick)

    def open_settings(self):
        self._flush()
        win=tk.Toplevel(self.root)
        win.title('Order Notes — Settings')
        self._dialog_geometry(win,'settings',
                              (min(500,max(360,win.winfo_screenwidth()-28)),
                               min(405,max(310,win.winfo_screenheight()-85))),
                              (400,330))
        win.minsize(min(400,win.winfo_screenwidth()-28),min(330,win.winfo_screenheight()-85))
        win.configure(bg=COLORS['white'])
        win.transient(self.root)
        win.grab_set()

        label(win,'Settings',size=13,bold=True).pack(anchor='w',padx=14,pady=(9,4))
        foot=tk.Frame(win,bg=COLORS['white'])
        foot.pack(side='bottom',fill='x',padx=13,pady=(4,9))
        tabs=ttk.Notebook(win,style='Modern.TNotebook')
        tabs.pack(fill='both',expand=True,padx=12,pady=(2,3))
        gateway_form=tk.Frame(tabs,bg=COLORS['white'])
        reminders_form=tk.Frame(tabs,bg=COLORS['white'])
        tabs.add(gateway_form,text='  SMS Gateway  ')
        tabs.add(reminders_form,text='  Reminders  ')
        vars={}

        def make_field(parent,key,title):
            wrapper=tk.Frame(parent,bg=COLORS['white'])
            label(wrapper,title,size=8,fg=COLORS['muted']).pack(anchor='w',pady=(0,1))
            value=self.gateway_password if key=='password_plain' else self.settings.get(key,'')
            value_var=tk.StringVar(value=value)
            vars[key]=value_var
            widget=entry(wrapper,value_var)
            if key=='password_plain':
                widget.configure(show='•')
            widget.pack(fill='x',ipady=2)
            return wrapper

        form=gateway_form
        make_field(form,'gateway_url','Android SMSGate URL').pack(fill='x',padx=9,pady=(10,7))
        credentials=tk.Frame(form,bg=COLORS['white'])
        credentials.pack(fill='x',padx=9,pady=(0,7))
        credentials.grid_columnconfigure(0,weight=1,uniform='gateway')
        credentials.grid_columnconfigure(1,weight=1,uniform='gateway')
        make_field(credentials,'gateway_user','Gateway username').grid(row=0,column=0,sticky='ew',padx=(0,5))
        make_field(credentials,'password_plain','Gateway password (this PC)').grid(row=0,column=1,sticky='ew',padx=(5,0))
        gateway_opts=tk.Frame(form,bg=COLORS['white'])
        gateway_opts.pack(fill='x',padx=9,pady=(0,7))
        gateway_opts.grid_columnconfigure(0,weight=1)
        gateway_opts.grid_columnconfigure(1,weight=1)
        make_field(gateway_opts,'country_code','Dialing country code').grid(row=0,column=0,sticky='ew',padx=(0,5))
        sim_box=tk.Frame(gateway_opts,bg=COLORS['white'])
        sim_box.grid(row=0,column=1,sticky='ew',padx=(5,0))
        label(sim_box,'Default SIM',size=8,fg=COLORS['muted']).pack(anchor='w',pady=(0,1))
        sim_var=tk.StringVar(value='SIM '+self.settings.get('default_sim','1'))
        ttk.Combobox(sim_box,values=['SIM 1','SIM 2'],textvariable=sim_var,
                     state='readonly',style='Modern.TCombobox').pack(fill='x')

        form=reminders_form
        make_field(form,'my_phone','My phone number (SMS reminders)').pack(fill='x',padx=9,pady=(10,9))
        intervals=tk.Frame(form,bg=COLORS['white'])
        intervals.pack(fill='x',padx=9,pady=(0,7))
        for i,(key,title) in enumerate((
            ('order_remind_days','Order after (days)'),
            ('arrival_remind_days','Notify after (days)'),
            ('repeat_remind_days','Repeat every (days)'),
        )):
            intervals.grid_columnconfigure(i,weight=1,uniform='interval')
            make_field(intervals,key,title).grid(row=0,column=i,sticky='ew',padx=(0 if i==0 else 4,0 if i==2 else 4))
        login_var=tk.BooleanVar(value=self.settings.get('run_at_login','0')=='1')
        tk.Checkbutton(form,text='Launch when I sign in to Windows',variable=login_var,
                       bg=COLORS['white'],selectcolor=COLORS['white'],font=('Segoe UI',9)).pack(anchor='w',padx=7,pady=(5,0))

        def collect():
            data={k:v.get().strip() for k,v in vars.items()}
            if data['gateway_url']:
                check_url(data['gateway_url'])
            if data['my_phone'] and not valid_phone(data['my_phone'],data['country_code']):
                raise ValueError('Enter a valid personal reminder phone number.')
            if not data['country_code'].isdigit() or len(data['country_code'])>3:
                raise ValueError('Default dialing code must have 1–3 digits.')
            for key in ('order_remind_days','arrival_remind_days','repeat_remind_days'):
                try:
                    number=float(data[key])
                except ValueError:
                    raise ValueError('Reminder intervals must be valid numbers of days.')
                if not 0.01 <= number <= 365:
                    raise ValueError('Reminder intervals must be between 0.01 and 365 days.')
            data['default_sim']=sim_var.get().split()[-1]
            data['run_at_login']='1' if login_var.get() else '0'
            return data

        def test():
            try:
                data=collect()
                gw=Gateway(data['gateway_url'],data['gateway_user'],data['password_plain'],data['country_code'])
            except (ValueError,GatewayError) as exc:
                messagebox.showwarning('Connection settings',str(exc),parent=win)
                return
            status.configure(text='Checking connection…',fg=COLORS['muted'])
            def do_test():
                try:
                    gw.health()
                    error=None
                except Exception as exc:
                    error=str(exc)
                def done():
                    if not status.winfo_exists():
                        return
                    if error:
                        status.configure(text='●  '+error,fg=COLORS['red'])
                    else:
                        status.configure(text='●  Gateway connected successfully',fg=COLORS['green'])
                        self.connection.set('●  Gateway connected')
                self._post_ui(done)
            threading.Thread(target=do_test,daemon=True).start()

        def save():
            try:
                data=collect()
                plaintext=data.pop('password_plain')
                # Password is encrypted per Windows login; never overwrite the
                # globally shared SQLite password with another user's DPAPI.
                store_local_gateway_password(plaintext)
                set_launch_at_login(login_var.get())
                self.db.set_settings(data)
                self.settings=self.db.settings()
                self.gateway_password=plaintext
                self.sim_var.set('SIM '+self.settings.get('default_sim','1'))
                self.banner.set('Settings saved successfully')
                win.destroy()
                self._after(500,self._reminder_tick)
            except (OSError,ValueError,GatewayError) as exc:
                messagebox.showerror('Cannot save Settings',str(exc),parent=win)

        status=label(foot,'Test connection does not send SMS',size=9,fg=COLORS['muted'])
        status.pack(anchor='w',pady=(0,7))
        button(foot,'Test connection',test,small=True).pack(side='left')
        button(foot,'Save settings',save,primary=True,small=True).pack(side='right')

    def edit_templates(self):
        win=tk.Toplevel(self.root)
        win.title('SMS templates')
        self._dialog_geometry(win,'templates',
                              (min(700,max(540,win.winfo_screenwidth()-30)),
                               min(440,max(330,win.winfo_screenheight()-90))),
                              (540,330))
        win.minsize(min(540,win.winfo_screenwidth()-30),min(330,win.winfo_screenheight()-90))
        win.configure(bg=COLORS['white'])
        win.transient(self.root)
        win.grab_set()
        label(win,'SMS templates',size=18,bold=True).pack(anchor='w',padx=20,pady=(17,4))
        label(win,'Edit Swedish messages. Only Arrival notification changes request status.',
              fg=COLORS['muted'],size=9).pack(anchor='w',padx=20,pady=(0,15))
        content=tk.Frame(win,bg=COLORS['white'])
        content.pack(fill='both',expand=True,padx=19,pady=(0,18))
        box=tk.Listbox(content,font=('Segoe UI',10),width=26,bg=COLORS['gray_light'],
                       relief='flat',highlightthickness=0,activestyle='none',selectbackground=COLORS['blue'])
        box.pack(side='left',fill='y',padx=(0,18))
        rhs=tk.Frame(content,bg=COLORS['white'])
        rhs.pack(side='left',fill='both',expand=True)
        label(rhs,'TEMPLATE TITLE',size=9,bold=True,fg=COLORS['muted']).pack(anchor='w')
        title_var=tk.StringVar()
        entry(rhs,title_var).pack(fill='x',ipady=6,pady=(5,11))
        label(rhs,'TEMPLATE TYPE',size=9,bold=True,fg=COLORS['muted']).pack(anchor='w')
        type_var=tk.StringVar(value='Other message')
        ttk.Combobox(rhs,values=['Arrival notification','Other message'],textvariable=type_var,
                     state='readonly',style='Modern.TCombobox').pack(fill='x',pady=(5,11))
        label(rhs,'SWEDISH MESSAGE',size=9,bold=True,fg=COLORS['muted']).pack(anchor='w')
        body=text_box(rhs,height=9)
        body.pack(fill='both',expand=True,pady=(5,12))
        label(rhs,'Use {items} to place the optional item list at a specific position.',
              fg=COLORS['muted'],size=9).pack(anchor='w',pady=(0,10))
        current_id=[None]
        def reload(select=None):
            values=self.db.templates()
            box.delete(0,'end')
            for tpl in values:
                box.insert('end',tpl['title'])
            if values:
                index=next((i for i,tpl in enumerate(values) if tpl['id']==select),0)
                box.selection_set(index)
                choose()
            else:
                new()
        def choose(e=None):
            sel=box.curselection()
            values=self.db.templates()
            if not sel or sel[0]>=len(values):
                return
            tpl=values[sel[0]]
            current_id[0]=tpl['id']
            title_var.set(tpl['title'])
            type_var.set('Arrival notification' if tpl['kind']=='arrival' else 'Other message')
            set_text(body,tpl['body'])
        box.bind('<<ListboxSelect>>',choose)
        def new():
            box.selection_clear(0,'end')
            current_id[0]=None
            title_var.set('')
            type_var.set('Other message')
            set_text(body,'')
        def save():
            try:
                kind='arrival' if type_var.get()=='Arrival notification' else 'other'
                self.db.save_template(title_var.get(),kind,text_get(body),current_id[0])
                self.refresh_templates()
                reload(current_id[0])
                self.banner.set('SMS template saved')
            except ValueError as exc:
                messagebox.showwarning('Invalid template',str(exc),parent=win)
        def remove():
            if current_id[0] is not None and messagebox.askyesno('Delete template','Permanently delete this SMS template?',parent=win):
                self.db.delete_template(current_id[0])
                self.refresh_templates()
                reload()
        actions=tk.Frame(rhs,bg=COLORS['white'])
        actions.pack(fill='x')
        button(actions,'+ New',new,small=True).pack(side='left')
        button(actions,'Delete',remove,small=True,danger=True).pack(side='left',padx=5)
        button(actions,'Save template',save,primary=True,small=True).pack(side='right')
        reload()

    def close(self):
        if not messagebox.askyesno('Exit Order Notes',
                'Do you want to close Order Notes?',icon='question',
                default='no',parent=self.root):
            return
        self._flush()
        save_status=self.save_label.cget('text')
        if save_status.startswith(('Not saved','Save failed','Draft backup failed')):
            if not messagebox.askyesno('Unsaved changes',
                    'Some changes could not be saved. Close anyway?\n\n'
                    'Choose No to return and check the request.',
                    icon='warning',default='no',parent=self.root):
                return
        try:
            self._save_layout()
        except OSError as exc:
            if not messagebox.askyesno('Window layout not saved',
                    f'Could not remember the window sizes:\n{exc}\n\nClose anyway?',
                    icon='warning',default='no',parent=self.root):
                return
        # From this point onward, background completions are discarded.
        # They may finish SQLite/SMSGate work, but cannot touch a destroyed Tk.
        self._closing=True
        for token in tuple(self._pending_after_ids):
            self._cancel_after(token)
        self.root.destroy()


def main():
    # Lock before creating Tk or opening the shared database. A second launch
    # on the SAME PC focuses the first window; other PCs may run independently.
    instance=SingleInstance()
    try:
        acquired=instance.acquire()
    except OSError as exc:
        if sys.platform=='win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,str(exc),'Order Notes',0x10)
        else:
            print('Cannot launch Order Notes:',exc,file=sys.stderr)
        return
    if not acquired:
        instance.notify_existing()
        return
    try:
        _run_app()
    finally:
        instance.close()


def _run_app():
    if sys.platform=='win32':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                'OrderNotes.Desktop')
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError,AttributeError):
            pass
    root=tk.Tk()
    asset_dir=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))/'assets'
    png_path=asset_dir/'order_notes.png'
    ico_path=asset_dir/'order_notes.ico'
    if png_path.exists():
        try:
            root._order_notes_icon=tk.PhotoImage(file=str(png_path))
            root.iconphoto(True,root._order_notes_icon)
        except tk.TclError:
            pass
    if sys.platform=='win32' and ico_path.exists():
        try:
            root.iconbitmap(default=str(ico_path))
        except tk.TclError:
            pass
    try:
        prepare_portable_storage(HOME)
        OrderNotesApp(root)
    except (OSError, sqlite3.DatabaseError) as exc:
        messagebox.showerror('Cannot open portable data',
            'Order Notes saves its database inside a data folder next to the program.\n\n'
            'This folder is not writable, or the existing database could not be migrated.\n'
            'Move the complete application folder to a writable location such as Documents '
            'and start Order Notes again.\n\n'
            'No existing data was deleted.\n\nDetails: '+str(exc),parent=root)
        root.destroy()
        return
    root.mainloop()


if __name__=='__main__':
    main()
