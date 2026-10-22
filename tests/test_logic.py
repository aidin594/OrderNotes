import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import Database
from gateway import Gateway, GatewayError, check_url
from sms_utils import estimate_sms
from phone import normalized_phone,valid_phone
from reminders import reminder_due, reminder_message,process_due_reminders


class PhoneTests(unittest.TestCase):
    def test_swedish_formats_match(self):
        self.assertEqual(normalized_phone('070-123 45 67','46'),'+46701234567')
        self.assertEqual(normalized_phone('+46 70 123 45 67','46'),'+46701234567')
        self.assertEqual(normalized_phone('0046701234567','46'),'+46701234567')

    def test_other_countries(self):
        self.assertEqual(normalized_phone('+989121234567','46'),'+989121234567')
        self.assertEqual(normalized_phone('0712345678','44'),'+44712345678')
        self.assertFalse(valid_phone('123','46'))

    def test_url_restrictions(self):
        self.assertEqual(check_url('http://192.168.1.10:8080/'),'http://192.168.1.10:8080')
        with self.assertRaises(GatewayError):
            check_url('http://example.com')
        with self.assertRaises(GatewayError):
            check_url('http://8.8.8.8')
        with self.assertRaises(GatewayError):
            check_url('https://user:password@192.168.1.1')

    def test_sms_segment_estimator(self):
        self.assertEqual(estimate_sms('Hej! Din beställning har kommit.')[2],1)
        self.assertEqual(estimate_sms('a'*161)[2],2)
        self.assertEqual(estimate_sms('🙂'*40)[2],2)

    def test_http_gateway_payload(self):
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import json
        import threading
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            def do_POST(self):
                seen.append((self.path,self.headers.get('Authorization'),json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(202)
                self.end_headers()
            def log_message(self,*args):
                pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        th=threading.Thread(target=server.serve_forever,daemon=True)
        th.start()
        try:
            g=Gateway(f'http://127.0.0.1:{server.server_port}','demo','password','46')
            self.assertEqual(g.health()['status'],'ok')
            self.assertTrue(g.send('0701234567','Hej!',2)['accepted'])
            self.assertEqual(seen[0][0],'/messages')
            self.assertTrue(seen[0][1].startswith('Basic '))
            self.assertEqual(seen[0][2]['phoneNumbers'],['+46701234567'])
            self.assertEqual(seen[0][2]['simNumber'],2)
        finally:
            server.shutdown()
            server.server_close()
            th.join(timeout=3)


class DBTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.tmp.name)/'data.db')
    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_filters(self):
        rid=self.db.new_request('0701234567',items='Nexus 7')
        self.assertEqual(self.db.get_request(rid)['stage'],'new')
        self.assertEqual(self.db.counts()['to_notify'],1)
        self.db.set_stage(rid,'ordered')
        self.db.set_stage(rid,'arrived')
        self.assertEqual(self.db.get_request(rid)['stage'],'arrived')
        self.assertEqual(len(self.db.requests('Nexus','to_notify')),1)
        self.assertEqual(len(self.db.requests('0701','to_notify')),1)
        self.db.set_stage(rid,'collected')
        self.assertEqual(self.db.counts()['completed'],1)
        self.assertEqual(len(self.db.requests(filter_name='to_notify')),0)
        self.assertGreaterEqual(len(self.db.activity(rid)[0]),4)

    def test_customer_arrival_only_changes_state_after_acceptance(self):
        rid=self.db.new_request('0701234567')
        a=self.db.begin_sms(rid,'0701234567',1,'Försenad','other')
        self.db.complete_sms(a,'queued')
        self.assertEqual(self.db.get_request(rid)['stage'],'new')
        b=self.db.begin_sms(rid,'0701234567',1,'Varorna finns','arrival')
        self.db.complete_sms(b,'uncertain')
        self.assertEqual(self.db.get_request(rid)['stage'],'new')
        c=self.db.begin_sms(rid,'0701234567',1,'Varorna finns','arrival')
        self.db.complete_sms(c,'queued')
        self.assertEqual(self.db.get_request(rid)['stage'],'notified')
        self.assertEqual(self.db.counts()['awaiting_pickup'],1)
        self.db.set_stage(rid,'new','Wrong message correction')
        self.assertEqual(self.db.get_request(rid)['stage'],'new')
        self.assertEqual(len(self.db.activity(rid)[1]),3)

    def test_optional_fields_and_backup(self):
        rid=self.db.new_request('0701234567')
        self.db.update_request(rid,'+46701234567','','new part\nand brake','confidential')
        self.assertEqual(self.db.get_request(rid)['items'],'new part\nand brake')
        self.assertEqual(self.db.get_request(rid)['phone_key'],'+46701234567')
        self.assertEqual(len(self.db.duplicate_phone('+46701234567')),1)
        backup=Path(self.tmp.name)/'snapshot.db'
        self.db.backup_to(backup)
        self.assertTrue(backup.exists())
        self.assertEqual(Database(backup).get_request(rid)['notes'],'confidential')

    def test_templates(self):
        t=self.db.templates()
        self.assertEqual(len(t),4)
        self.assertEqual(t[0]['kind'],'arrival')
        self.db.save_template('Another','other','Hej!')
        self.assertEqual(len(self.db.templates()),5)
        self.db.delete_template(self.db.templates()[-1]['id'])
        self.assertEqual(len(self.db.templates()),4)

    def test_reminder_schedule(self):
        rid=self.db.new_request('0701234567',items='A 7vxl shifter')
        req=self.db.get_request(rid)
        start=datetime.fromisoformat(req['stage_changed_at'])
        c=self.db.settings()
        self.assertFalse(reminder_due(req,None,c,start+timedelta(days=2)))
        self.assertTrue(reminder_due(req,None,c,start+timedelta(days=4)))
        msg=reminder_message(req)
        self.assertIn('0701234567',msg)
        self.assertIn('7vxl',msg)
        last={'occurred_at':start.isoformat(),'status':'sent'}
        self.assertTrue(reminder_due(req,last,c,start+timedelta(days=4)))
        uncertain={'occurred_at':start.isoformat(),'status':'uncertain'}
        self.assertFalse(reminder_due(req,uncertain,c,start+timedelta(days=50)))
        self.db.set_stage(rid,'ordered')
        req=self.db.get_request(rid)
        self.assertFalse(reminder_due(req,None,c,start+timedelta(days=50)))

    def test_automatic_reminder_only_to_self_once(self):
        rid=self.db.new_request('0701234567',items='Brompton tyre')
        with self.db.connection() as c:
            c.execute('UPDATE requests SET stage_changed_at=? WHERE id=?',((datetime.now().astimezone()-timedelta(days=10)).isoformat(),rid))
        conf=self.db.settings()
        conf.update(gateway_url='http://192.168.1.10:8080',gateway_user='u',my_phone='0709876543',default_sim='2')
        calls=[]
        class DummyGateway:
            def __init__(self,*args,**kwargs):
                pass
            def send(self,number,message,sim):
                calls.append((number,message,sim))
                return {'accepted':True}
        with patch('reminders.Gateway',DummyGateway):
            self.assertEqual(process_due_reminders(self.db,conf,'password'),1)
            self.assertEqual(process_due_reminders(self.db,conf,'password'),0)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0],'0709876543')
        self.assertEqual(calls[0][2],2)
        self.assertEqual(self.db.get_request(rid)['stage'],'new')

    def test_unknown_result_does_not_auto_retry(self):
        rid=self.db.new_request('0701234567')
        with self.db.connection() as c:
            c.execute('UPDATE requests SET stage_changed_at=? WHERE id=?',((datetime.now().astimezone()-timedelta(days=20)).isoformat(),rid))
        conf=self.db.settings()
        conf.update(gateway_url='http://192.168.1.10:8080',gateway_user='u',my_phone='0709876543')
        class BadGateway:
            def __init__(self,*args,**kwargs): pass
            def send(self,*args): raise GatewayError('Network timeout',uncertain=True)
        with patch('reminders.Gateway',BadGateway):
            process_due_reminders(self.db,conf,'pw')
            self.assertEqual(process_due_reminders(self.db,conf,'pw'),0)
        self.assertEqual(self.db.last_reminder(rid,'new',self.db.get_request(rid)['stage_changed_at'])['status'],'uncertain')

if __name__=='__main__':
    unittest.main()

class UpgradeAndDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.tmp.name)/'orders.db')

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_gateway_is_not_replaced_by_private_defaults(self):
        self.db.set_settings({'gateway_url':'','gateway_user':'','default_sim':'1',
                              'gateway_password':'dpapi:example-user-protected-value'})
        self.db.prefill_existing_gateway()
        settings=self.db.settings()
        self.assertEqual(settings['gateway_url'],'')
        self.assertEqual(settings['gateway_user'],'')
        self.assertEqual(settings['default_sim'],'1')
        self.assertEqual(settings['gateway_password'],'dpapi:example-user-protected-value')
        # Later user settings are never re-overwritten.
        self.db.set_settings({'gateway_url':'http://192.168.1.25:8080','default_sim':'1'})
        self.db.prefill_existing_gateway()
        self.assertEqual(self.db.settings()['gateway_url'],'http://192.168.1.25:8080')
        self.assertEqual(self.db.settings()['default_sim'],'1')

    def test_delete_request_cascades_all_associated_records(self):
        rid=self.db.new_request('0701234567',items='One part')
        keep=self.db.new_request('0709876543',items='Keep part')
        sid=self.db.log_sms(rid,'0701234567',2,'Hej!','arrival','customer','queued')
        self.db.log_reminder(rid,'new','sent',sid,'Reminder delivered')
        self.assertTrue(self.db.delete_request(rid))
        self.assertFalse(self.db.delete_request(rid))
        self.assertIsNone(self.db.get_request(rid))
        self.assertIsNotNone(self.db.get_request(keep))
        with self.db.connection() as conn:
            for tab in ('sms_log','events','reminder_log'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {tab} WHERE request_id=?',(rid,)).fetchone()[0],0)
