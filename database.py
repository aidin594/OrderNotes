"""Local SQLite storage. Independent of UI, safe to use in worker threads."""
import os
import sqlite3
from contextlib import contextmanager, closing
from datetime import datetime
from pathlib import Path

from phone import normalized_phone

STAGES = ('new', 'ordered', 'arrived', 'notified', 'collected')
STAGE_LABELS = {
    'new': 'To order', 'ordered': 'Ordered', 'arrived': 'Ready to notify',
    'notified': 'Customer notified', 'collected': 'Collected',
}

DEFAULT_TEMPLATES = [
    ('Order has arrived', 'arrival', 'Hej! Din beställning har kommit och finns nu att hämta hos butiken.\n\nVälkommen!'),
    ('Order placed', 'other', 'Hej! Vi har beställt varan du frågade efter. Vi hör av oss när den har kommit.\n\nbutiken'),
    ('Order delayed', 'other', 'Hej! Din beställning är tyvärr försenad. Vi återkommer så snart vi har mer information.\n\nbutiken'),
    ('Pickup reminder', 'other', 'Hej! En vänlig påminnelse om att din beställning finns att hämta hos butiken.\n\nVälkommen!'),
]

DEFAULT_SETTINGS = {
    'gateway_url': '', 'gateway_user': '', 'gateway_password': '',
    'my_phone': '', 'country_code': '46', 'default_sim': '1',
    'order_remind_days': '3', 'arrival_remind_days': '1',
    'repeat_remind_days': '3', 'run_at_login': '0',
}


def now():
    # Sub-second precision helps distinguish edits from different workstations.
    return datetime.now().astimezone().isoformat(timespec='microseconds')


class ConcurrentUpdateError(RuntimeError):
    """A different workstation modified or deleted this request."""



class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=30)
        try:
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA busy_timeout = 30000')
            con.execute('PRAGMA foreign_keys = ON')
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _initialize(self):
        with self.connection() as c:
            # WAL shared-memory index does not work across computers on a network share.
            # Use the rollback journal for both local and mapped/UNC paths so
            # a host opening the folder locally uses the SAME journal mode.
            c.execute('PRAGMA journal_mode = DELETE')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY,
                    phone TEXT NOT NULL, phone_key TEXT NOT NULL,
                    customer_name TEXT NOT NULL DEFAULT '',
                    items TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT 'new',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    stage_changed_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    ordered_at TEXT, arrived_at TEXT, notified_at TEXT, collected_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_req_stage ON requests(stage);
                CREATE INDEX IF NOT EXISTS idx_req_phone ON requests(phone_key);
                CREATE TABLE IF NOT EXISTS templates (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('arrival','other')),
                    body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                    occurred_at TEXT NOT NULL, event_type TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS sms_log (
                    id INTEGER PRIMARY KEY,
                    request_id INTEGER REFERENCES requests(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL, recipient TEXT NOT NULL, sim INTEGER NOT NULL,
                    message TEXT NOT NULL, kind TEXT NOT NULL,
                    purpose TEXT NOT NULL, status TEXT NOT NULL,
                    result_detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS reminder_log (
                    id INTEGER PRIMARY KEY,
                    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    status TEXT NOT NULL, sms_log_id INTEGER REFERENCES sms_log(id),
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            ''')
            # Existing v1.x databases are upgraded in place without data loss.
            columns={r['name'] for r in c.execute('PRAGMA table_info(requests)')}
            if 'revision' not in columns:
                c.execute('ALTER TABLE requests ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            c.executemany('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', list(DEFAULT_SETTINGS.items()))
            if not c.execute('SELECT 1 FROM templates LIMIT 1').fetchone():
                c.executemany('INSERT INTO templates(title,kind,body) VALUES(?,?,?)', DEFAULT_TEMPLATES)

    def prefill_existing_gateway(self):
        """One-time migration of empty v1.x settings, without overwriting changes.

        An existing configured gateway and its selected SIM are left untouched.
        """
        with self.connection() as c:
            if c.execute("SELECT 1 FROM settings WHERE key='gateway_prefilled_v14'").fetchone():
                return
            settings = {r['key']:r['value'] for r in c.execute('SELECT key,value FROM settings')}
            if not settings.get('gateway_url'):
                c.execute("UPDATE settings SET value=? WHERE key='gateway_url'", (DEFAULT_SETTINGS['gateway_url'],))
            if not settings.get('gateway_user'):
                c.execute("UPDATE settings SET value=? WHERE key='gateway_user'", (DEFAULT_SETTINGS['gateway_user'],))
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('gateway_prefilled_v14','1')")

    def settings(self):
        with self.connection() as c:
            return {r['key']: r['value'] for r in c.execute('SELECT * FROM settings')}

    def set_settings(self, values):
        allowed = set(DEFAULT_SETTINGS)
        with self.connection() as c:
            c.executemany('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          [(k, str(v)) for k, v in values.items() if k in allowed])

    def templates(self):
        with self.connection() as c:
            return [dict(r) for r in c.execute('SELECT * FROM templates ORDER BY id')]

    def save_template(self, title, kind, body, template_id=None):
        if not title.strip() or not body.strip() or kind not in ('other','arrival'):
            raise ValueError('Enter a title, SMS body and valid template type.')
        with self.connection() as c:
            if template_id is None:
                c.execute('INSERT INTO templates(title,kind,body) VALUES(?,?,?)', (title,kind,body))
            else:
                c.execute('UPDATE templates SET title=?,kind=?,body=? WHERE id=?', (title,kind,body,template_id))

    def delete_template(self, template_id):
        with self.connection() as c:
            c.execute('DELETE FROM templates WHERE id=?',(template_id,))

    def new_request(self, phone, name='', items='', notes='', country='46'):
        from phone import valid_phone
        if not valid_phone(phone, country):
            raise ValueError('Enter a valid phone number (6–16 digits).')
        timestamp = now()
        with self.connection() as c:
            cur = c.execute('''INSERT INTO requests(phone,phone_key,customer_name,items,notes,stage,created_at,updated_at,stage_changed_at)
                            VALUES(?,?,?,?,?,'new',?,?,?)''',
                            (phone.strip(), normalized_phone(phone,country),name,items,notes,timestamp,timestamp,timestamp))
            request_id = cur.lastrowid
            self._event(c, request_id, 'Created', 'Request registered',timestamp)
            return request_id

    def get_request(self, req_id):
        with self.connection() as c:
            row = c.execute('SELECT * FROM requests WHERE id=?',(req_id,)).fetchone()
            return dict(row) if row else None

    def requests(self, text='', filter_name='to_notify'):
        filter_sql = {
            'to_notify': "AND stage IN ('new','ordered','arrived')",
            'awaiting_pickup': "AND stage='notified'",
            'completed': "AND stage='collected'",
            'all': '',
        }.get(filter_name,'')
        search = f'%{text.strip()}%'
        with self.connection() as c:
            q = ("SELECT * FROM requests WHERE (phone LIKE ? OR phone_key LIKE ? "
                 "OR customer_name LIKE ? OR items LIKE ? OR notes LIKE ?) " + filter_sql +
                 " ORDER BY CASE WHEN stage='collected' THEN 1 ELSE 0 END, created_at DESC, id DESC")
            return [dict(r) for r in c.execute(q,[search]*5)]

    def counts(self):
        with self.connection() as c:
            rows = c.execute('SELECT stage,COUNT(*) AS n FROM requests GROUP BY stage').fetchall()
            ct = {r['stage']:r['n'] for r in rows}
            return {
                'to_notify':sum(ct.get(v,0) for v in ('new','ordered','arrived')),
                'awaiting_pickup':ct.get('notified',0),
                'completed':ct.get('collected',0),
                'all':sum(ct.values()),
            }

    def duplicate_phone(self, phone_key, exclude_id=None):
        with self.connection() as c:
            q = 'SELECT id,items,stage FROM requests WHERE phone_key=? AND stage!=\'collected\''
            args = [phone_key]
            if exclude_id:
                q += ' AND id!=?'
                args.append(exclude_id)
            return [dict(r) for r in c.execute(q,args)]

    def update_request(self, req_id, phone, name, items, notes, country='46', expected_revision=None):
        from phone import valid_phone
        if not valid_phone(phone,country):
            raise ValueError('Enter a valid phone number (6–16 digits).')
        timestamp=now()
        with self.connection() as c:
            sql='''UPDATE requests SET phone=?,phone_key=?,customer_name=?,items=?,notes=?,updated_at=?,
                   revision=revision+1 WHERE id=?'''
            args=[phone.strip(),normalized_phone(phone,country),name,items,notes,timestamp,req_id]
            if expected_revision is not None:
                sql+=' AND revision=?'
                args.append(expected_revision)
            result=c.execute(sql,args)
            if result.rowcount!=1:
                raise ConcurrentUpdateError('Request changed or was deleted on another computer.')
            return expected_revision+1 if expected_revision is not None else None

    def _event(self,c,request_id,typ,detail='',timestamp=None):
        c.execute('INSERT INTO events(request_id,occurred_at,event_type,detail) VALUES(?,?,?,?)',
                  (request_id,timestamp or now(),typ,detail))

    def set_stage(self, req_id, stage, detail='Manual status change', expected_revision=None):
        if stage not in STAGES:
            raise ValueError('Unknown status')
        timestamp = now()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT stage,revision FROM requests WHERE id=?',(req_id,)).fetchone()
            if expected_revision is not None and (not old or old['revision']!=expected_revision):
                raise ConcurrentUpdateError('Request changed or was deleted on another computer.')
            if not old or old['stage'] == stage:
                return False
            stamps = {'ordered':'ordered_at', 'arrived':'arrived_at', 'notified':'notified_at', 'collected':'collected_at'}
            c.execute('UPDATE requests SET stage=?,stage_changed_at=?,updated_at=?,revision=revision+1 WHERE id=?',
                      (stage,timestamp,timestamp,req_id))
            if stage in stamps:
                c.execute(f'UPDATE requests SET {stamps[stage]}=? WHERE id=?',(timestamp,req_id))
            self._event(c,req_id,'Status changed',f"{STAGE_LABELS[old['stage']]} → {STAGE_LABELS[stage]} · {detail}",timestamp)
            return True

    def log_sms(self, req_id, recipient, sim, message, kind, purpose, status, detail=''):
        timestamp = now()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            cur=c.execute('''INSERT INTO sms_log(request_id,created_at,recipient,sim,message,kind,purpose,status,result_detail)
                             VALUES(?,?,?,?,?,?,?,?,?)''',
                          (req_id,timestamp,recipient,sim,message,kind,purpose,status,detail))
            sms_id=cur.lastrowid
            self._event(c,req_id, 'SMS '+status, f'{purpose} → {recipient} · {detail}',timestamp)
            if purpose=='customer' and kind=='arrival' and status=='queued':
                req=c.execute('SELECT stage FROM requests WHERE id=?',(req_id,)).fetchone()
                if req and req['stage']!='collected' and req['stage']!='notified':
                    c.execute('UPDATE requests SET stage=?,stage_changed_at=?,updated_at=?,notified_at=?,revision=revision+1 WHERE id=?',
                              ('notified',timestamp,timestamp,timestamp,req_id))
                    self._event(c,req_id,'Status changed','Arrival SMS accepted by gateway',timestamp)
            return sms_id

    def log_reminder(self, req_id, stage, status, sms_id=None, detail=''):
        with self.connection() as c:
            c.execute('''INSERT INTO reminder_log(request_id,stage,occurred_at,status,sms_log_id,detail)
                         VALUES(?,?,?,?,?,?)''',(req_id,stage,now(),status,sms_id,detail))

    def last_reminder(self, req_id, stage, since):
        with self.connection() as c:
            row=c.execute('''SELECT * FROM reminder_log WHERE request_id=? AND stage=? AND occurred_at>=?
                             ORDER BY id DESC LIMIT 1''',(req_id,stage,since)).fetchone()
            return dict(row) if row else None

    def activity(self, req_id):
        with self.connection() as c:
            events=[dict(r) for r in c.execute('SELECT occurred_at,event_type,detail FROM events WHERE request_id=?',(req_id,))]
            sms=[dict(r) for r in c.execute('SELECT * FROM sms_log WHERE request_id=? ORDER BY id DESC',(req_id,))]
            return sorted(events,key=lambda v:v['occurred_at'],reverse=True),sms

    def delete_request(self, req_id, expected_revision=None):
        with self.connection() as c:
            sql='DELETE FROM requests WHERE id=?'
            args=[req_id]
            if expected_revision is not None:
                sql+=' AND revision=?'
                args.append(expected_revision)
            result=c.execute(sql,args)
            if expected_revision is not None and result.rowcount!=1:
                raise ConcurrentUpdateError('Request changed or was deleted on another computer.')
            return result.rowcount == 1

    def backup_to(self, target):
        path=Path(target)
        path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as source, closing(sqlite3.connect(path)) as dest:
            source.backup(dest)

    def reserve_reminder(self, req_id, stage, expected_last_id=None):
        """Atomically reserve a reminder across several running PCs."""
        timestamp = now()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT stage,stage_changed_at FROM requests WHERE id=?',(req_id,)).fetchone()
            if not row or row['stage']!=stage:
                return None
            latest=c.execute('''SELECT id FROM reminder_log
                 WHERE request_id=? AND stage=? AND occurred_at>=?
                 ORDER BY id DESC LIMIT 1''',(req_id,stage,row['stage_changed_at'])).fetchone()
            actual=latest['id'] if latest else None
            if actual!=expected_last_id:
                return None
            cur=c.execute('''INSERT INTO reminder_log(request_id,stage,occurred_at,status)
                             VALUES(?,?,?,'started')''',(req_id,stage,timestamp))
            return cur.lastrowid

    def finish_reminder(self, reminder_id, status, sms_id=None, detail=''):
        with self.connection() as c:
            c.execute('UPDATE reminder_log SET status=?,sms_log_id=?,detail=? WHERE id=?',
                      (status,sms_id,detail,reminder_id))

    def begin_sms(self, req_id, recipient, sim, message, kind='other', purpose='customer'):
        timestamp=now()
        with self.connection() as c:
            cur=c.execute('''INSERT INTO sms_log(request_id,created_at,recipient,sim,message,kind,purpose,status,result_detail)
                             VALUES(?,?,?,?,?,?,?,'started','')''',
                          (req_id,timestamp,recipient,sim,message,kind,purpose))
            self._event(c,req_id,'SMS started',f'{purpose} → {recipient}',timestamp)
            return cur.lastrowid

    def complete_sms(self, sms_id, status, detail=''):
        timestamp=now()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            entry=c.execute('SELECT * FROM sms_log WHERE id=?',(sms_id,)).fetchone()
            if not entry or entry['status']!='started':
                return
            c.execute('UPDATE sms_log SET status=?,result_detail=? WHERE id=?',(status,detail,sms_id))
            self._event(c,entry['request_id'],'SMS '+status,f"{entry['purpose']} → {entry['recipient']} · {detail}",timestamp)
            if entry['purpose']=='customer' and entry['kind']=='arrival' and status=='queued':
                stage=c.execute('SELECT stage FROM requests WHERE id=?',(entry['request_id'],)).fetchone()
                if stage and stage['stage'] not in ('notified','collected'):
                    c.execute('''UPDATE requests SET stage='notified',stage_changed_at=?,notified_at=?,updated_at=?,revision=revision+1 WHERE id=?''',
                              (timestamp,timestamp,timestamp,entry['request_id']))
                    self._event(c,entry['request_id'],'Status changed','Arrival SMS accepted by gateway',timestamp)
