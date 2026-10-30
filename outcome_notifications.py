"""Broker-confirmed outcomes, calendar reports and a recipient-level durable outbox."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
import csv
import hashlib
import html
import io
import json
import math
import smtplib
from zoneinfo import ZoneInfo
from email_notifications import SMTPSettings, deliver_message

TZ = ZoneInfo('Europe/Stockholm')
UTC = timezone.utc


def stamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def pid(value):
    return str(value or '').removesuffix('.0')


def preferences(conn):
    values = dict(conn.execute("SELECT key,value FROM app_settings WHERE key IN ('closure_email_enabled','weekly_closed_report_enabled')"))
    return {key: values.get(key, 'true') == 'true' for key in ('closure_email_enabled', 'weekly_closed_report_enabled')}


def ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS outcome_notification_events(
        event_key TEXT PRIMARY KEY,kind TEXT NOT NULL,created_at TEXT NOT NULL,
        payload TEXT NOT NULL,baseline INTEGER NOT NULL DEFAULT 0,recipients_seeded INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS outcome_notification_deliveries(
        id INTEGER PRIMARY KEY,event_key TEXT NOT NULL REFERENCES outcome_notification_events(event_key),
        recipient TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'PENDING',attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,updated_at REAL NOT NULL DEFAULT 0,
        message_id TEXT NOT NULL,last_error TEXT,accepted_at TEXT,
        UNIQUE(event_key,recipient));
    """)
    # Existing closures become a baseline, not a flood of historical alerts.
    now = datetime.now(UTC).isoformat()
    first = conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES ('outcome_notifications_started_at',?)", (now,)).rowcount
    if first:
        for row in conn.execute('SELECT position_id FROM broker_closed_positions').fetchall():
            conn.execute("INSERT OR IGNORE INTO outcome_notification_events(event_key,kind,created_at,payload,baseline) VALUES (?,'closure',?,'{}',1)",
                         ('closure:' + pid(row[0]), now))
    for key in ('closure_email_enabled', 'weekly_closed_report_enabled'):
        conn.execute('INSERT OR IGNORE INTO app_settings(key,value) VALUES (?,?)', (key, 'true'))


def closure_reason(conn, trade, broker, closed):
    """Separate explicit broker reasons from Finbot intent; never infer from P/L."""
    labels={'stoploss':'Stop-loss', 'sl':'Stop-loss', 'takeprofit':'Take-profit',
            'tp':'Take-profit', 'manual':'Manual closure', 'manualclose':'Manual closure',
            'userrequested':'Manual closure'}
    raw=broker.get('raw') or {}
    for source in (broker,raw):
        for key in ('close_reason','closeReason','closeReasonName','closureReason'):
            value=source.get(key)
            if isinstance(value,str):
                normalized=''.join(ch for ch in value.lower() if ch.isalpha())
                if normalized in labels:
                    return dict(closure_reason=labels[normalized],closure_reason_basis='broker_reported',
                                closure_reason_detail='Explicitly reported by the broker.')
    if trade.get('id') and conn.execute("SELECT 1 FROM sqlite_master WHERE name='broker_actions'").fetchone():
        for action in conn.execute("SELECT * FROM broker_actions WHERE trade_id=? AND kind='close' AND attempts>0 AND order_id IS NOT NULL AND state IN ('CLOSED','ACKNOWLEDGED','EXECUTION_REPORTED') ORDER BY last_attempt_at DESC",(trade['id'],)):
            action=dict(action)
            if pid(action['position_id'])!=pid(trade.get('broker_position_id')):continue
            if closed is None or action['last_attempt_at'] is None or float(action['last_attempt_at'])>closed.timestamp():continue
            try:payload=json.loads(action['payload'] or '{}')
            except (TypeError,ValueError):continue
            reason=payload.get('reason','')
            if reason=='profit_trailing:drawdown':
                evidence=payload.get('profit_trailing') or {}
                label='Profit trailing exit'
                if evidence.get('basis')=='entry_atr_v1':
                    detail=(f"{str(evidence.get('tier') or 'Unknown').title()} {'amount' if evidence.get('arming_basis')=='amount' else 'ATR'} tier; arming {evidence.get('arming_basis','atr')}; peak USD {evidence.get('peak_profit_usd','not applicable')}; entry {evidence.get('entry_price')}; entry ATR {evidence.get('entry_atr')}; best price {evidence.get('best_price')}; "
                        f"price at close check {evidence.get('current_price')}; retreat {evidence.get('retreat_atr')} ATR; permitted retreat {evidence.get('allowed_retreat_atr')} ATR; trigger price {evidence.get('trigger_price')}; "
                        f"reported P/L at check USD {evidence.get('profit_usd','unavailable')}.")
                else:
                    detail=f"{str(evidence.get('tier') or 'Unknown').title()} tier; peak USD {evidence.get('peak_profit_usd','unavailable')}; profit at close check USD {evidence.get('profit_usd','unavailable')}; decline USD {evidence.get('drawdown_usd','unavailable')}; trigger profit USD {evidence.get('trigger_profit_usd','unavailable')}."
            else:
                label={'stop_level_observed':'Stop-loss level observed',
                       'take_profit_level_observed':'Take-profit level observed',
                       'profit_review:structural_news_invalidated_thesis':'News invalidated the thesis',
                       'profit_review:actual_fill_exceeded_configured_units_or_spend_cap':'Fill exceeded the configured size/spend cap',
                       'User requested closure; awaiting broker confirmation.':'Manual closure'}.get(reason,'Other Finbot-requested closure')
                detail='Recorded close request: '+str(reason)[:160]
            return dict(closure_reason=label,closure_reason_basis='finbot_request',
                        closure_reason_detail=detail+' Finbot request recorded; broker confirms closure but did not identify the execution trigger.')
    return dict(closure_reason='Unknown',closure_reason_basis='unknown',
                closure_reason_detail='Broker history did not specify a reason and no matching submitted Finbot close request was found. Profit or exit price alone does not establish SL/TP or manual closure.')


def closed_outcomes(conn, start=None, end=None, include_undated=False):
    trades = {}
    for raw in conn.execute("SELECT * FROM trades WHERE broker_position_id IS NOT NULL ORDER BY id"):
        trade = dict(raw)
        trades.setdefault(pid(trade['broker_position_id']), trade)
    unique = {}
    for raw in conn.execute('SELECT * FROM broker_closed_positions'):
        row = dict(raw)
        key = pid(row['position_id'])
        # Prefer a known P/L if legacy identifier spellings collide.
        if key not in unique or finite(row['net_profit']) is not None:
            unique[key] = row
    outcomes = []
    undated = 0
    for key, row in unique.items():
        closed = stamp(row['closed_at'])
        if closed is None:
            undated += 1
            if not include_undated:continue
        if closed is not None and (start is not None and closed < start or end is not None and closed >= end):
            continue
        trade = trades.get(key, {})
        try:
            broker = json.loads(row['payload'] or '{}')
        except (ValueError, TypeError):
            broker = {}
        raw = broker.get('raw') or {}
        outcomes.append(dict(
            position_id=key, trade_id=trade.get('id'),
            symbol=trade.get('symbol') or raw.get('internalSymbolFull') or raw.get('symbol') or 'Unlinked',
            side=trade.get('side') or 'Unknown', closed_at=closed.isoformat() if closed else None,
            opened_at=trade.get('opened_at'), entry_price=finite(trade.get('broker_fill_price')) or finite(trade.get('entry_price')),
            close_price=finite(broker.get('close_rate')) or finite(trade.get('close_rate')),
            units=finite(broker.get('units')) or finite(trade.get('broker_fill_units')) or finite(trade.get('quantity')),
            net_profit=finite(row['net_profit']), reported_fees=finite(row['fees']),
            **closure_reason(conn,trade,broker,closed)))
    outcomes.sort(key=lambda row: row['closed_at'] or '', reverse=True)
    return outcomes, undated


def summary(rows):
    pnl = [Decimal(str(r['net_profit'])) for r in rows if r['net_profit'] is not None]
    gains = sum((p for p in pnl if p > 0), Decimal(0))
    losses = -sum((p for p in pnl if p < 0), Decimal(0))
    return dict(currency='USD',closed_trades=len(rows),known_net_outcomes=len(pnl),
        missing_net_outcomes=len(rows)-len(pnl),wins=sum(p>0 for p in pnl),losses=sum(p<0 for p in pnl),
        break_even=sum(p==0 for p in pnl),winning_gains=float(round(gains,2)),
        losing_losses=float(round(losses,2)),net_profit=float(round(gains-losses,2)))


def report(conn, start=None, end=None):
    rows, undated = closed_outcomes(conn,start,end)
    return dict(window_start=start.isoformat() if start else None,window_end=end.isoformat() if end else None,
        timezone='Europe/Stockholm',summary=summary(rows),outcomes=rows,undated_history_excluded=undated,
        generated_at=datetime.now(UTC).isoformat(),
        scope='Broker-confirmed closed positions recorded by Finbot; unlinked account history may be absent.',
        broker_history_sync=dict(conn.execute("SELECT key,value FROM app_settings WHERE key IN ('closed_history_synced_at','closed_history_complete')")),
        note='Net P/L is broker-reported. Reported fees are not subtracted again. Open P/L is excluded.')


def queue_closure(conn, position_id, now=None):
    key = 'closure:' + pid(position_id)
    if conn.execute('SELECT 1 FROM outcome_notification_events WHERE event_key=?',(key,)).fetchone():
        return False
    rows, _ = closed_outcomes(conn,include_undated=True)
    rows = [row for row in rows if row['position_id']==pid(position_id)]
    if not rows:
        return False  # no broker-confirmed outcome recorded yet
    payload = dict(outcomes=rows,summary=summary(rows),note='Broker-confirmed closure. Net P/L includes broker-reported costs; fees are not subtracted twice.')
    cur = conn.execute("INSERT OR IGNORE INTO outcome_notification_events(event_key,kind,created_at,payload,baseline) VALUES (?,'closure',?,?,?)",
        (key,(now or datetime.now(UTC)).isoformat(),json.dumps(payload),int(not preferences(conn)['closure_email_enabled'])))
    return bool(cur.rowcount)


def queue_weekly(conn, now=None, require_fresh_history=False):
    if not preferences(conn)['weekly_closed_report_enabled']:
        return False
    now = now or datetime.now(UTC)
    if require_fresh_history:
        sync=dict(conn.execute("SELECT key,value FROM app_settings WHERE key IN ('closed_history_synced_at','closed_history_complete')"))
        synced=stamp(sync.get('closed_history_synced_at'))
        if sync.get('closed_history_complete')!='true' or synced is None or not 0<=(now-synced).total_seconds()<=86400:
            return False
    local = now.astimezone(TZ)
    monday = local.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=local.weekday())
    # Monday 09:00 local; a restart later in the week catches up once.
    if local < monday.replace(hour=9):
        return False
    if require_fresh_history and synced < monday.astimezone(UTC):
        return False
    start = monday-timedelta(days=7)
    key = 'weekly:' + start.date().isoformat()
    if conn.execute('SELECT 1 FROM outcome_notification_events WHERE event_key=?',(key,)).fetchone():
        return False
    payload = report(conn,start.astimezone(UTC),monday.astimezone(UTC))
    return bool(conn.execute("INSERT OR IGNORE INTO outcome_notification_events(event_key,kind,created_at,payload) VALUES (?,'weekly',?,?)",
        (key,now.isoformat(),json.dumps(payload))).rowcount)


def csv_report(rows):
    fields = ['position_id','trade_id','symbol','side','opened_at','closed_at','units','entry_price','close_price','net_profit','reported_fees','closure_reason','closure_reason_basis','closure_reason_detail']
    output = io.StringIO()
    writer = csv.DictWriter(output,fieldnames=fields)
    writer.writeheader()
    for row in rows:
        # Avoid spreadsheet formulas in externally supplied text.
        clean = {key:row.get(key) for key in fields}
        for key in ('position_id','symbol','side','opened_at','closed_at','closure_reason','closure_reason_basis','closure_reason_detail'):
            if isinstance(clean[key],str) and clean[key].startswith(('=','+','-','@')):
                clean[key] = "'" + clean[key]
        writer.writerow(clean)
    return output.getvalue()


def build_message(settings,event,message_id):
    payload=json.loads(event['payload'])
    stats=payload['summary']
    if event['kind']=='closure':
        row=payload['outcomes'][0]
        amount='P/L unavailable' if row['net_profit'] is None else f"USD {row['net_profit']:+,.2f}"
        title=f"Trade closed: {row['symbol']} · {amount}"
    else:
        start=stamp(payload['window_start']).astimezone(TZ)
        end=stamp(payload['window_end']).astimezone(TZ)-timedelta(days=1)
        title=f"Weekly closed trades: {start:%d %b}–{end:%d %b %Y}"
    lines=[title]
    if event['kind']=='weekly':
        lines.extend([f"Winning-trade gains: USD {stats['winning_gains']:,.2f}",
            f"Losing-trade losses: USD {stats['losing_losses']:,.2f}",
            f"Net realized P/L: USD {stats['net_profit']:+,.2f}",
            f"Closed trades: {stats['closed_trades']} · Wins: {stats['wins']} · Losses: {stats['losses']} · Break-even: {stats['break_even']}",
            f"Missing net outcomes: {stats['missing_net_outcomes']}",
            'Window: previous Monday 00:00 through Monday 00:00, Europe/Stockholm (end excluded).'])
    for row in payload['outcomes']:
        amount='Unavailable' if row['net_profit'] is None else f"USD {row['net_profit']:+,.2f}"
        closed=stamp(row['closed_at'])
        closed_label=closed.astimezone(TZ).strftime('%Y-%m-%d %H:%M %Z') if closed else 'Unavailable (broker timestamp missing)'
        lines.append(f"{row['symbol']} {row['side']} · position {row['position_id']} · closed {closed_label} · net {amount} · units {row['units']} · entry {row['entry_price']} · exit {row['close_price']} · reported fees {row['reported_fees']}")
        lines.append('Closure reason: '+row.get('closure_reason','Unknown')+' · source: '+row.get('closure_reason_basis','unknown'))
        lines.append(row.get('closure_reason_detail','Reason was not captured in this saved report.'))
    if not payload['outcomes']:
        lines.append('No broker-confirmed closed trades in this period.')
    lines.append(payload['note'])
    if payload.get('scope'):lines.append(payload['scope'])
    lines.append('SMTP acceptance does not confirm inbox delivery.')
    message=EmailMessage()
    message['Subject']=settings.subject_prefix+' '+title
    message['From']=settings.sender
    message['To']=', '.join(settings.recipients)
    message['Message-ID']=message_id
    message['Date']=format_datetime(datetime.now(UTC))
    message.set_content('\n'.join(lines))
    message.add_alternative('<html><body><h2>'+html.escape(title)+'</h2><pre>'+html.escape('\n'.join(lines[1:]))+'</pre></body></html>',subtype='html')
    if event['kind']=='weekly':
        message.add_attachment(csv_report(payload['outcomes']).encode(),maintype='text',subtype='csv',filename='finbot-closed-trades.csv')
    if settings.attach_json:
        message.add_attachment(json.dumps(payload,indent=2).encode(),maintype='application',subtype='json',filename='finbot-outcome-evidence.json')
    return message


def process_outbox(connect, settings=None, now=None, send=deliver_message, limit=10):
    settings=settings or SMTPSettings.from_environment()
    if not settings.enabled:return {'disabled':True}
    now=now or datetime.now(UTC);epoch=now.timestamp()
    sender_address=parseaddr(settings.sender)[1]
    message_domain=sender_address.rsplit('@',1)[1] if '@' in sender_address else 'finbot.local'
    c=connect()
    try:
        # A crash after DATA starts is ambiguous: never automatically resend.
        c.execute("UPDATE outcome_notification_deliveries SET state='UNCERTAIN',last_error='Interrupted after SMTP send began' WHERE state='SENDING' AND updated_at<?",(epoch-600,))
        c.execute("UPDATE outcome_notification_deliveries SET state='RETRY' WHERE state='PREPARING' AND updated_at<?",(epoch-600,))
        prefs=preferences(c)
        allowed=[kind for kind,key in [('closure','closure_email_enabled'),('weekly','weekly_closed_report_enabled')] if prefs[key]]
        for event in c.execute("SELECT * FROM outcome_notification_events WHERE baseline=0 AND recipients_seeded=0").fetchall():
            if event['kind'] not in allowed:continue
            for recipient in settings.recipients:
                digest=hashlib.sha256((event['event_key']+'\0'+recipient).encode()).hexdigest()[:40]
                c.execute("INSERT OR IGNORE INTO outcome_notification_deliveries(event_key,recipient,message_id) VALUES (?,?,?)",
                    (event['event_key'],recipient,f'<finbot-{digest}@{message_domain}>'))
            c.execute('UPDATE outcome_notification_events SET recipients_seeded=1 WHERE event_key=?',(event['event_key'],))
        c.commit()
        rows=c.execute("""SELECT d.*,e.kind,e.payload FROM outcome_notification_deliveries d
            JOIN outcome_notification_events e ON e.event_key=d.event_key
            WHERE d.state IN ('PENDING','RETRY') AND d.next_attempt_at<=?
            ORDER BY d.id LIMIT ?""",(epoch,limit)).fetchall()
    finally:c.close()
    result={}
    for raw in rows:
        row=dict(raw)
        if row['kind'] not in allowed:continue
        c=connect()
        try:
            claim=c.execute("UPDATE outcome_notification_deliveries SET state='PREPARING',attempts=attempts+1,updated_at=? WHERE id=? AND state IN ('PENDING','RETRY')",
                (epoch,row['id'])).rowcount
            c.commit()
        finally:c.close()
        if not claim:continue
        started=False
        def before_data():
            nonlocal started
            c=connect()
            try:
                c.execute("UPDATE outcome_notification_deliveries SET state='SENDING',updated_at=? WHERE id=?",(datetime.now(UTC).timestamp(),row['id']))
                c.commit()
            finally:c.close()
            started=True
        try:
            single=replace(settings,recipients=(row['recipient'],))
            event=dict(kind=row['kind'],payload=row['payload'])
            sent=send(single,build_message(single,event,row['message_id']),before_data=before_data)
            if sent.get('status')!='sent':
                raise smtplib.SMTPRecipientsRefused({row['recipient']:(550,b'Refused')})
            state='SENT';error=None;accepted=datetime.now(UTC).isoformat()
        except Exception as exc:
            code=getattr(exc,'smtp_code',None)
            if isinstance(exc,smtplib.SMTPRecipientsRefused):
                codes=[int(v[0]) for v in exc.recipients.values()]
                code=max(codes) if codes else 550
            error=type(exc).__name__+(f': {int(code)}' if code else '')
            accepted=None
            known_rejection=isinstance(exc,(smtplib.SMTPRecipientsRefused,smtplib.SMTPDataError,smtplib.SMTPSenderRefused))
            if started and not known_rejection:
                state='UNCERTAIN'
            elif (code and int(code)>=500) or row['attempts']+1>=5:
                state='FAILED'
            else:state='RETRY'
        c=connect()
        try:
            c.execute("UPDATE outcome_notification_deliveries SET state=?,last_error=?,accepted_at=?,updated_at=?,next_attempt_at=? WHERE id=?",
                (state,error,accepted,datetime.now(UTC).timestamp(),epoch+min(3600,60*2**row['attempts']),row['id']))
            c.commit()
        finally:c.close()
        result[state]=result.get(state,0)+1
    return result
