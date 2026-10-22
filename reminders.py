"""Automatic, persistent, stage-specific SMS reminders.

No unattended retry after a POST timeout, because the SMS might be queued already.
One overdue SMS at most on launch regardless of days spent offline.
"""
from datetime import datetime, timedelta
from database import now
from gateway import Gateway, GatewayError

RETRY_WAIT = timedelta(minutes=15)


def reminder_due(req, last, config, moment=None):
    moment = moment or datetime.now().astimezone()
    stage=req['stage']
    if stage not in ('new','arrived'):
        return False
    first_days = float(config.get('order_remind_days','3') if stage=='new' else config.get('arrival_remind_days','1'))
    repeat_days = float(config.get('repeat_remind_days','3'))
    entered=datetime.fromisoformat(req['stage_changed_at'])
    if not last:
        return moment >= entered+timedelta(days=first_days)
    sent=datetime.fromisoformat(last['occurred_at'])
    if last['status']=='sent':
        return moment >= sent+timedelta(days=repeat_days)
    if last['status']=='failed':
        return moment >= sent+RETRY_WAIT
    # 'started' could mean crash after send; 'uncertain' could mean timeout after acceptance.
    return False


def reminder_message(req):
    phone=req['phone']
    items=(req['items'] or '').strip()
    description=('\nVaror: '+items[:350]) if items else ''
    if req['stage']=='new':
        return 'Påminnelse från Order Notes: Du har ännu inte beställt varorna till kund '+phone+'.'+description
    return 'Påminnelse från Order Notes: Beställningen har kommit. Glöm inte att skicka SMS till kund '+phone+'.'+description


def process_due_reminders(db, config, password, on_result=None):
    """Run in a worker. Returns number accepted. Does not depend on the UI thread."""
    my_number=(config.get('my_phone') or '').strip()
    if not my_number or not config.get('gateway_url') or not config.get('gateway_user') or not password:
        return 0
    gateway=Gateway(config['gateway_url'],config['gateway_user'],password,config.get('country_code','46'))
    sim=int(config.get('default_sim','1'))
    sent=0
    for req in db.requests(filter_name='to_notify'):
        if req['stage'] not in ('new','arrived'):
            continue
        last=db.last_reminder(req['id'],req['stage'],req['stage_changed_at'])
        if not reminder_due(req,last,config):
            continue
        # Reserve before contacting gateway; prevents duplicates after a crash.
        log_id=db.reserve_reminder(req['id'],req['stage'],last['id'] if last else None)
        if log_id is None:
            continue
        # Another workstation may have processed this request after the
        # initial list query. Do not send a now-irrelevant reminder.
        current=db.get_request(req['id'])
        if not current or current['stage']!=req['stage']:
            db.finish_reminder(log_id,'cancelled',detail='Status changed before SMS send')
            continue
        msg=reminder_message(req)
        try:
            result=gateway.send(my_number,msg,sim)
            sms_id=db.log_sms(req['id'],my_number,sim,msg,'reminder','self_reminder','queued',str(result)[:300])
            db.finish_reminder(log_id,'sent',sms_id,'Accepted by gateway; actual delivery not guaranteed')
            sent+=1
            if on_result:
                on_result('Reminder queued to your phone')
        except GatewayError as exc:
            status='uncertain' if exc.uncertain else 'failed'
            sms_id=db.log_sms(req['id'],my_number,sim,msg,'reminder','self_reminder',status,str(exc))
            db.finish_reminder(log_id,status,sms_id,str(exc))
            if on_result:
                on_result('Reminder issue: '+str(exc))
            # When gateway unreachable, don't try every overdue request at once.
            if exc.uncertain or 'unavailable' in str(exc).lower() or 'authentication' in str(exc).lower():
                break
    return sent
