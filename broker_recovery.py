"""Durable broker-action recovery, owned by the existing Execution workflow.

No global request budget. Each action has a persisted next check, bounded writes,
request correlation and one attention event. Uncertain closes are never resent.
"""
import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
import position_policy
import position_timing
from market_sessions import build_market_session_context

TERMINAL = ('APPLIED', 'CLOSED', 'SUPERSEDED', 'RESOLVED')


def ensure_schema(conn):
    conn.executescript('''CREATE TABLE IF NOT EXISTS broker_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER NOT NULL REFERENCES trades(id),
        position_id TEXT, kind TEXT NOT NULL, state TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, next_check_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, checks INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0, mismatches INTEGER NOT NULL DEFAULT 0,
        order_id TEXT, request_id TEXT, payload TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '{}',
        last_error TEXT, alert_at REAL, last_attempt_at REAL,
        UNIQUE(request_id));
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_broker_action ON broker_actions(trade_id,kind)
        WHERE state NOT IN ('APPLIED','CLOSED','SUPERSEDED','RESOLVED');
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_position_action ON broker_actions(position_id,kind)
        WHERE position_id<>'' AND state NOT IN ('APPLIED','CLOSED','SUPERSEDED','RESOLVED');
        CREATE INDEX IF NOT EXISTS due_broker_actions ON broker_actions(next_check_at,state);
    ''')


def safe(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if any(word in str(k).lower() for word in
            ('token','password','secret','authorization','userkey','apikey')) else safe(v) for k,v in value.items()}
    if isinstance(value, list): return [safe(v) for v in value[:100]]
    if isinstance(value,str):return re.sub(r'(?i)(api[_-]?key|user[_-]?key|token|secret|password)=([^&\s]+)',r'\1=[REDACTED]',value)
    if isinstance(value, (int,float,bool)) or value is None: return value
    return str(value)


def decode(row):
    if not row: return None
    result=dict(row)
    for key in ('payload','evidence'): result[key]=json.loads(result[key] or '{}')
    return result


def list_actions(conn, trade_id=None, active_only=False):
    clauses=[];params=[]
    if trade_id is not None: clauses.append('trade_id=?');params.append(trade_id)
    if active_only: clauses.append("state NOT IN ('APPLIED','CLOSED','SUPERSEDED','RESOLVED')")
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    return [decode(r) for r in conn.execute('SELECT * FROM broker_actions'+where+' ORDER BY id DESC',params)]


def epoch(value, default):
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).timestamp()
    except (TypeError, ValueError): return default


def weekend(now):
    # Slower reads for stock/ETF weekend queues; never suppress a first risk exit.
    return datetime.fromtimestamp(now,timezone.utc).weekday()>=5


def business_age(start, now):
    """Exclude weekend days from age-only escalation, not from error alerts."""
    total=0
    while start<now:
        day=datetime.fromtimestamp(start,timezone.utc)
        end=min(now,(day.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).timestamp())
        if day.weekday()<5: total+=end-start
        start=end
    return total


def close_order_id(evidence):
    if not isinstance(evidence,dict): return None
    for key in ('order_id','orderID','orderId'):
        if evidence.get(key): return str(evidence[key]).removesuffix('.0')
    for key in ('broker_close','orderForClose','close','broker_response','response','result'):
        found=close_order_id(evidence.get(key))
        if found: return found
    return None


def named_status(data):
    value=data.get('status') or data.get('statusName') or data.get('orderStatus')
    if isinstance(value,dict): value=value.get('name') or {1:'received',2:'placed',3:'filled',4:'rejected',5:'partiallyfilled',6:'pendingcancel',7:'canceled',8:'expired',9:'canceledpartiallyfilled',10:'rejectedpartiallyfilled',11:'waitingformarket',12:'pendingtriggeredrate'}.get(value.get('id'),'')
    return str(value or '').lower().replace(' ','').replace('_','')


class RecoveryManager:
    def __init__(self, connection_factory, record_event, rules=None, clock=time.time):
        self.connect=connection_factory;self.event=record_event;self.rules=rules or {};self.clock=clock

    def get(self, trade_id, kind):
        c=self.connect()
        try:
            return decode(c.execute("SELECT * FROM broker_actions WHERE trade_id=? AND kind=? AND state NOT IN ('APPLIED','CLOSED','SUPERSEDED','RESOLVED') ORDER BY id DESC LIMIT 1",(trade_id,kind)).fetchone())
        finally: c.close()

    def update(self, action, **fields):
        fields['updated_at']=self.clock()
        c=self.connect()
        try:
            stored={k:json.dumps(safe(v)) if k in ('payload','evidence') else safe(v) for k,v in fields.items()}
            c.execute('UPDATE broker_actions SET '+','.join(k+'=?' for k in stored)+' WHERE id=?',(*stored.values(),action['id']))
            c.commit()
        finally: c.close()
        action.update(fields)
        return action

    def delay(self, action, minimum=0):
        initial=float(self.rules.get('recovery_initial_check_seconds',60))
        maximum=float(self.rules.get('recovery_max_check_seconds',900))
        delay=min(maximum,initial*2**min(action['checks']+action['failures'],6))
        if weekend(self.clock()) and action['kind']!='protection': delay=max(delay,900)
        jitter=int(hashlib.sha256(f"{action['id']}:{action['checks']}:{action['attempts']}".encode()).hexdigest()[:6],16)%max(1,int(delay*.2))
        return max(minimum,delay+jitter)

    def attention(self, action, message):
        first=not action.get('alert_at')
        self.update(action,alert_at=action.get('alert_at') or self.clock(),last_error=message[:1000])
        if first:
            self.event(action['trade_id'],'recovery','position_manager','attention','attention',message,
                {'action_id':action['id'],'kind':action['kind'],'state':action['state']})

    def create(self, trade, kind, payload, state='READY', created_at=None, order_id=None, request_id=None, attempts=0, stagger=False):
        c=self.connect();now=self.clock()
        try:
            c.execute('BEGIN IMMEDIATE')
            position_id=str(trade.get('broker_position_id') or '').removesuffix('.0')
            row=c.execute("SELECT * FROM broker_actions WHERE (trade_id=? OR (position_id<>'' AND position_id=?)) AND kind=? AND state NOT IN ('APPLIED','CLOSED','SUPERSEDED','RESOLVED')",(trade['id'],position_id,kind)).fetchone()
            if row:
                c.commit();return decode(row)
            due=now+(int(hashlib.sha256(f"{trade['id']}:{kind}".encode()).hexdigest()[:6],16)%300 if stagger else 0)
            cur=c.execute('INSERT INTO broker_actions(trade_id,position_id,kind,state,created_at,updated_at,next_check_at,order_id,request_id,payload,attempts,last_attempt_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (trade['id'],str(trade.get('broker_position_id') or '').removesuffix('.0'),kind,state,created_at or now,now,due,order_id,request_id,json.dumps(safe(payload)),attempts,created_at if attempts else None))
            row=c.execute('SELECT * FROM broker_actions WHERE id=?',(cur.lastrowid,)).fetchone();c.commit();return decode(row)
        finally:c.close()

    def finish(self, action, state, message):
        self.update(action,state=state,alert_at=None,last_error=None)
        self.event(action['trade_id'],'recovery','position_manager','completed','accepted',message,
            {'action_id':action['id'],'kind':action['kind'],'state':state})

    def import_legacy(self, trade, position=None):
        """Import acknowledged historical operations; never re-send on migration."""
        now=self.clock();policy=json.loads(trade.get('policy_json') or '{}')
        cancellation=self.get(trade['id'],'entry_cancel')
        if position is not None and cancellation:self.finish(cancellation,'RESOLVED','Verified portfolio fill wins cancellation race; position retained and protected.')
        if trade['status']=='EXIT_PENDING' and not self.get(trade['id'],'close'):
            c=self.connect()
            try: events=c.execute("SELECT created_at,details,message FROM trade_events WHERE trade_id=? AND stage='exit' ORDER BY id DESC",(trade['id'],)).fetchall()
            finally:c.close()
            chosen=None;oid=None
            for e in events:
                data=json.loads(e['details'] or '{}');found=close_order_id(data)
                if found:chosen=e;oid=found;break
            created=epoch(chosen['created_at'],now) if chosen else now
            self.create(trade,'close',{'reason':chosen['message'] if chosen else 'legacy_close_intent','instrument_id':(position or {}).get('instrument_id')},
                state='ACKNOWLEDGED' if oid else 'UNKNOWN',created_at=created,order_id=oid,attempts=1,stagger=True)
        if position is not None and policy.get('last_protection_request_signature') and not self.get(trade['id'],'protection'):
            try:payload=json.loads(policy['last_protection_request_signature'])
            except (TypeError,ValueError):return
            if not isinstance(payload,dict):return
            if position and self.protection_matches(payload,position):return
            self.create(trade,'protection',payload,state='ACKNOWLEDGED',created_at=float(policy.get('last_protection_request') or now),attempts=1,stagger=True)

    @staticmethod
    def protection_matches(payload, position):
        native=payload.get('stop_loss_type')=='trailing'
        if native and position.get('stop_loss_type')!='trailing':return False
        for key in ('stop_loss_price','take_profit_price'):
            wanted=payload.get(key);actual=position.get(key)
            if wanted is None:continue
            if actual is None:return False
            wanted=float(wanted);actual=float(actual)
            if not all(math.isfinite(v) and v>0 for v in (wanted,actual)):return False
            tolerance=max(.01,abs(wanted)*.0001)
            if native and key=='stop_loss_price':
                if position.get('side') not in {'LONG','SHORT'}:return False
                if (actual<wanted-tolerance if position['side']=='LONG' else actual>wanted+tolerance):return False
            elif abs(wanted-actual)>tolerance:return False
        return True

    def bind_position(self, action, trade, position):
        """Repair only a missing identity proven by the linked trade + fresh portfolio."""
        observed=str(position.get('position_id') or '').removesuffix('.0')
        linked=str(trade.get('broker_position_id') or '').removesuffix('.0')
        if action['trade_id']!=trade['id'] or not observed or linked!=observed or (action.get('position_id') and action['position_id']!=observed):
            if action['state']=='ESCALATED' and action['next_check_at']>self.clock():return False
            self.update(action,state='ESCALATED')
            self.attention(action,'Recovery position identity is inconsistent; broker writes are blocked.')
            self.schedule(action)
            return False
        if not action.get('position_id'):
            try:self.update(action,position_id=observed)
            except sqlite3.IntegrityError:
                self.update(action,state='ESCALATED')
                self.attention(action,'Another active action owns this position; duplicate recovery is blocked.')
                self.schedule(action)
                return False
            self.event(trade['id'],'recovery','position_manager','completed','accepted',
                'Missing recovery position ID repaired from the linked trade and broker portfolio.',
                {'action_id':action['id'],'position_id':observed})
        return True

    def adopt_tighter_protection(self, action, trade, position):
        if action['payload'].get('stop_loss_type')=='trailing':return False
        # A never-submitted intent cannot race an outstanding correction request.
        if action['attempts'] or action['state'] not in {'READY','RETRY_READY','ESCALATED'}:
            return False
        actual=position.get('stop_loss_price');wanted=action['payload'].get('stop_loss_price')
        if trade['side'] not in {'LONG','SHORT'} or actual is None or wanted is None or position.get('side',trade['side'])!=trade['side']:
            return False
        actual=float(actual);wanted=float(wanted)
        tighter=actual>wanted+.01 if trade['side']=='LONG' else actual<wanted-.01
        if not math.isfinite(actual) or actual<=0 or not tighter:
            return False
        payload={**action['payload'],'stop_loss_price':actual}
        if position.get('stop_loss_type')=='trailing':payload['stop_loss_type']='trailing'
        target_matches=self.protection_matches(payload,position)
        previous=action['payload']
        c=self.connect()
        try:
            c.execute('UPDATE trades SET stop_loss_price=? WHERE id=?',(actual,trade['id']))
            c.commit()
        finally:c.close()
        trade['stop_loss_price']=actual
        self.update(action,payload=payload)
        if target_matches:
            self.finish(action,'APPLIED','Tighter broker protection retained; no widening request was sent.')
        else:
            self.update(action,state='RETRY_READY',next_check_at=self.clock(),alert_at=None,last_error=None,evidence={'tighter_stop_adopted':True})
        self.event(trade['id'],'protection','position_manager','completed','accepted',
            'Adopted verified tighter broker stop; target reconciliation remains independent; no stop widening.',
            {'action_id':action['id'],'previous_intent':previous,'broker_stop':actual})
        return target_matches

    async def send(self, action, trade, position, adapter):
        now=self.clock();maximum=int(self.rules.get('recovery_max_attempts',3))
        if action['trade_id']!=trade['id']:
            return {'deduplicated':True,'action_id':action['id'],'owner_trade_id':action['trade_id'],'state':action['state']}
        if action['state']=='ESCALATED':raise ValueError('Tracked broker action needs attention; no repeat request was sent.')
        if action['state'] not in {'READY','RETRY_READY'} or action['next_check_at']>now:return {'deduplicated':True,'action_id':action['id'],'state':action['state']}
        if action['attempts']>=maximum:
            self.update(action,state='ESCALATED');self.attention(action,'Broker action reached its write-attempt limit; verification continues without resubmission.')
            return {'deduplicated':True,'action_id':action['id'],'state':action['state']}
        if not self.bind_position(action,trade,position):raise ValueError('Recovery position identity mismatch')
        if action['kind']=='protection' and action['payload'].get('native_atr'):
            if not await self.authorize_native_protection(action,trade,adapter):
                self.finish(action,'SUPERSEDED','Native trailing update withheld by fresh identity, mode or price check.')
                return {'withheld':True,'recovery_state':'SUPERSEDED'}
        if action['kind']=='close' and action['payload'].get('reason','').startswith('profit_trailing:'):
            if not await self.authorize_trailing_close(action, trade, adapter):
                self.finish(action, 'SUPERSEDED', 'Profit trailing close withheld: fresh position no longer meets the drawdown trigger.')
                c = self.connect()
                try:
                    c.execute("UPDATE trades SET status='OPEN' WHERE id=? AND status='EXIT_PENDING'", (trade['id'],))
                    c.commit()
                finally:
                    c.close()
                return {'withheld': True, 'recovery_state': 'SUPERSEDED'}
        if action['kind']=='close' and action['payload'].get('reason','').startswith('profit_review:'):
            if not await self.authorize_profit_close(action,trade,position,adapter):
                self.finish(action,'SUPERSEDED','Discretionary close withheld: fresh net-profit, session or review guard did not pass.')
                c=self.connect()
                try:
                    c.execute("UPDATE trades SET status=CASE WHEN status='EXIT_PENDING' THEN 'OPEN' ELSE status END,reconciliation_status=CASE WHEN status='EXIT_PENDING' THEN 'profit_close_withheld' ELSE reconciliation_status END WHERE id=?",(trade['id'],));c.commit()
                finally:c.close()
                return {'withheld':True,'recovery_state':'SUPERSEDED'}
        request_id=str(uuid.uuid4())
        self.update(action,state='SUBMITTING',attempts=action['attempts']+1,order_id=None,request_id=request_id,last_attempt_at=now)
        # Persist intent before transport. A crash after this commit is UNKNOWN,
        # never evidence that it is safe to repeat a closing order.
        c=self.connect()
        try:
            if action['kind']=='close':
                c.execute("UPDATE trades SET status='EXIT_PENDING',reconciliation_status='close_submission_pending' WHERE id=?",(trade['id'],))
            else:
                p=action['payload']
                c.execute("UPDATE trades SET stop_loss_price=?,take_profit_price=COALESCE(?,take_profit_price),status=CASE WHEN status='EXIT_PENDING' THEN status ELSE 'RECONCILIATION_REQUIRED' END,reconciliation_status=CASE WHEN status='EXIT_PENDING' THEN reconciliation_status ELSE 'protection_update_pending' END,stop_state='broker_verification_pending' WHERE id=?",(p['stop_loss_price'],p.get('take_profit_price'),trade['id']))
            c.commit()
        finally:c.close()
        try:
            if action['kind']=='close':
                response=await asyncio.to_thread(adapter.close_position,action['position_id'],position.get('instrument_id'),request_id=request_id)
            else:
                extra={'stop_type':'trailing'} if action['payload'].get('stop_loss_type')=='trailing' else {}
                response=await asyncio.to_thread(adapter.modify_position,action['position_id'],action['payload']['stop_loss_price'],action['payload'].get('take_profit_price'),request_id=request_id,**extra)
            if not isinstance(response,dict):raise ValueError('Broker acknowledgment unavailable')
            self.update(action,state='ACKNOWLEDGED',order_id=close_order_id(response) if action['kind']=='close' else None,
                evidence=response,last_error=None,mismatches=0,next_check_at=now+self.delay(action))
            c=self.connect()
            try:
                c.execute("UPDATE trades SET reconciliation_status=CASE WHEN status='EXIT_PENDING' AND ?='protection' THEN reconciliation_status ELSE ? END WHERE id=?",(action['kind'],'close_submitted' if action['kind']=='close' else 'protection_update_pending',trade['id']));c.commit()
            finally:c.close()
            self.event(trade['id'],'exit' if action['kind']=='close' else 'protection','position_manager','submitted','pending',
                action['payload'].get('reason','Tracked protection update accepted; awaiting broker verification.'),{**safe(response),'recovery_action_id':action['id']})
            return {**response,'recovery_action_id':action['id'],'recovery_state':action['state']}
        except Exception as error:
            code=getattr(error,'status_code',None)
            if code is None:
                m=re.search(r'HTTP (\d{3})',str(error));code=int(m[1]) if m else None
            wait=float(getattr(error,'retry_after_seconds',None) or 0)
            state='RETRY_READY' if code==429 else 'ESCALATED' if code in {400,401,403,404,422} else 'UNKNOWN'
            self.update(action,state=state,failures=action['failures']+1,last_error=f'{type(error).__name__}: {str(error)[:800]}',next_check_at=now+self.delay(action,max(wait,300 if state=='UNKNOWN' else 0)))
            if state=='ESCALATED':self.update(action,evidence={'definitively_rejected':True,'http_status':code})
            if state=='ESCALATED':self.attention(action,'Broker rejected the action; automatic resubmission stopped: '+str(error)[:500])
            self.event(trade['id'],'recovery','position_manager','waiting','uncertain' if state=='UNKNOWN' else 'deferred',
                'Broker action was not confirmed; outcome is tracked before any recovery attempt.',{'action_id':action['id'],'state':state,'http_status':code})
            raise

    async def authorize_native_protection(self,action,trade,adapter):
        """Native stop changes use a fresh broker position and frozen entry basis."""
        if not self.rules.get('profit_trailing_enabled') or self.rules.get('profit_trailing_mode','scheduled')!='broker':return False
        if getattr(adapter,'supports_native_trailing',False) is not True:return False
        try:
            snapshot=await asyncio.to_thread(adapter.get_positions)
            matches=[p for p in snapshot.get('positions',[]) if str(p.get('position_id')).removesuffix('.0')==action['position_id']]
            if not snapshot.get('available') or len(matches)!=1:return False
            position=matches[0]
            c=self.connect()
            try:row=c.execute('SELECT position_id,side,entry_price,entry_atr,best_price,evidence FROM price_trailing WHERE trade_id=?',(trade['id'],)).fetchone()
            finally:c.close()
            if not row or row[0]!=action['position_id'] or row[1]!=trade['side']:return False
            rate=await asyncio.to_thread(adapter.get_rate,int(position['instrument_id']))
            evidence=position_policy.trailing_evidence(trade,position,rate,row[4],self.rules,
                entry_price=row[2],entry_atr=row[3],now=datetime.fromtimestamp(self.clock(),timezone.utc),previous_evidence=json.loads(row[5]))
            intent=position_policy.native_trailing_intent(evidence,position)
            if not intent:return False
            target=action['payload'].get('take_profit_price')
            if target is not None:
                mark=evidence['current_price'];target=float(target)
                if not math.isfinite(target) or (target<=mark if trade['side']=='LONG' else target>=mark):return False
            # Re-enabling/retuning an already-native stop must actually tighten it.
            actual=position.get('stop_loss_price')
            if position.get('stop_loss_type')=='trailing' and actual is not None:
                improved=intent['stop_loss_price']>float(actual)+.01 if trade['side']=='LONG' else intent['stop_loss_price']<float(actual)-.01
                if not improved:return False
            c=self.connect()
            try:
                c.execute('UPDATE price_trailing SET best_price=?,evidence=? WHERE trade_id=?',
                    (evidence['best_price'],json.dumps(evidence),trade['id']));c.commit()
            finally:c.close()
            self.update(action,payload={**action['payload'],**intent},evidence={'native_guard':evidence})
            return True
        except Exception as error:
            self.update(action,evidence={'native_guard_unavailable':type(error).__name__})
            return False

    async def authorize_trailing_close(self, action, trade, adapter):
        """Revalidate identity and entry-ATR price retreat before every close attempt."""
        if not self.rules.get('profit_trailing_enabled', False):
            return False
        try:
            snapshot = await asyncio.to_thread(adapter.get_positions)
            matches = [p for p in snapshot.get('positions', [])
                       if str(p.get('position_id')).removesuffix('.0') == action['position_id']]
            if not snapshot.get('available') or len(matches) != 1:
                return False
            c = self.connect()
            try:
                row = c.execute('SELECT position_id,side,entry_price,entry_atr,best_price,evidence FROM price_trailing WHERE trade_id=?', (trade['id'],)).fetchone()
            finally:
                c.close()
            if not row or row[0] != action['position_id'] or row[1] != trade.get('side'):
                return False
            rate = await asyncio.to_thread(adapter.get_rate, int(matches[0]['instrument_id']))
            evidence = position_policy.trailing_evidence(trade, matches[0], rate, row[4], self.rules, entry_price=row[2], entry_atr=row[3], now=datetime.fromtimestamp(self.clock(),timezone.utc), previous_evidence=json.loads(row[5]))
            if self.rules.get('profit_trailing_mode','scheduled')=='broker':
                import native_trailing
                if native_trailing.confirmed_protection(evidence,matches[0]):
                    self.update(action,evidence={'native_broker_owns_exit':True,'profit_trailing':evidence})
                    return False
            c = self.connect()
            try:
                c.execute('UPDATE price_trailing SET best_price=?,evidence=? WHERE trade_id=? AND position_id=?',
                    (evidence['best_price'], json.dumps(evidence), trade['id'], action['position_id']))
                c.commit()
            finally:
                c.close()
            self.update(action, payload={**action['payload'], 'profit_trailing': evidence}, evidence=evidence)
            return evidence['close_due']
        except Exception as error:
            self.update(action, evidence={'trailing_guard_unavailable': type(error).__name__})
            return False

    async def authorize_profit_close(self,action,trade,position,adapter):
        """Recheck every attempted write, including retries after confirmed rejection."""
        try:
            from integrations import fetch_company_fundamentals
            snapshot=await asyncio.to_thread(adapter.get_positions)
            matches=[p for p in snapshot.get('positions',[]) if str(p.get('position_id')).removesuffix('.0')==action['position_id']]
            if not snapshot.get('available') or len(matches)!=1:return False
            current=matches[0]
            fundamentals=await fetch_company_fundamentals(trade['symbol'])
            rate=await asyncio.to_thread(adapter.get_rate,int(current['instrument_id']))
            now=datetime.fromtimestamp(self.clock(),timezone.utc)
            session=build_market_session_context(fundamentals=fundamentals,quote={'available':True},observed_at=now)
            evidence=position_policy.profit_exit_evidence(trade,current,rate,self.rules,now)
            evidence['market_session']=session
            reason=action['payload']['reason']
            if reason.endswith('structural_news_invalidated_thesis'):
                c=self.connect()
                try:r=c.execute("SELECT created_at,decision,details FROM trade_events WHERE trade_id IN (SELECT id FROM trades WHERE symbol=?) AND stage='research' ORDER BY id DESC LIMIT 1",(trade['symbol'],)).fetchone()
                finally:c.close()
                if not r or r['decision']!='accepted':return False
                research=json.loads(r['details']);ai=research.get('openai') or {}
                if ai.get('status')!='available' or ai.get('company_relevance')!='direct':return False
                max_age=min(24,float(json.loads(trade.get('config_json') or '{}').get('execution',{}).get('max_research_age_hours',24)))
                if not position_timing.invalidation_reason(trade,research,r['created_at'],now,max_age_hours=max_age):return False
            elif reason.endswith('actual_fill_exceeded_configured_units_or_spend_cap'):
                caps=json.loads(trade.get('config_json') or '{}').get('execution',{})
                if not (abs(float(current['units']))>float(caps.get('max_order_units',5))+.000001 or float(current['amount'])+max(0,float(current['fees']))>float(caps.get('max_order_spend_usd',200))+.01):return False
            else:return False
            self.update(action,evidence=evidence,payload={**action['payload'],'profit_guard':evidence})
            return evidence['allowed'] and session['calendar_status']=='provider_session_confirmed' and session['is_regular_session'] and (session.get('minutes_to_close') or 0)>10
        except Exception as error:
            self.update(action,evidence={'profit_guard_unavailable':type(error).__name__})
            return False

    async def request_close(self, trade, position, adapter, reason):
        action=self.get(trade['id'],'close') or self.create(trade,'close',{'reason':reason,'instrument_id':position.get('instrument_id')})
        return await self.send(action,trade,position,adapter)

    async def request_protection(self, trade, position, adapter, stop, target, *, explicit=False, stop_type=None, native_evidence=None):
        self.import_legacy(trade,position)
        action=self.get(trade['id'],'protection')
        payload={'stop_loss_price':float(stop),'take_profit_price':float(target) if target is not None else None}
        if stop_type not in (None,'fixed','trailing'):raise ValueError('Unsupported stop-loss type')
        if stop_type=='trailing' or stop_type is None and position.get('stop_loss_type')=='trailing':
            payload['stop_loss_type']='trailing'
        if native_evidence is not None:
            payload.update(native_atr=True,profit_trailing=native_evidence)
        if action and action['payload']!=payload:
            # Do not race a different intent against an unconfirmed async update.
            if action['state']=='ESCALATED' and action.get('evidence',{}).get('definitively_rejected'):
                self.finish(action,'SUPERSEDED','Rejected protection intent replaced by an explicit revised request.');action=None
            else:raise ValueError('A different protection update is still unresolved; inspect broker recovery status.')
        if not action and self.protection_matches(payload,position):
            return {'already_applied':True,'recovery_state':'APPLIED'}
        action=action or self.create(trade,'protection',payload)
        actual=position.get('stop_loss_price')
        loosens=actual is not None and (float(actual)>float(stop)+.01 if trade['side']=='LONG' else float(actual)<float(stop)-.01)
        if loosens and not explicit and action['state']=='READY':
            self.update(action,state='ESCALATED',evidence={'not_submitted':True})
            self.attention(action,'Broker stop is already tighter than the stored intent; automatic correction will not loosen it.')
        if explicit and action['state']=='ESCALATED' and action['evidence'].get('not_submitted'):
            self.update(action,state='READY',alert_at=None)
        return await self.send(action,trade,position,adapter)

    def schedule(self, action, *, state=None, error=None, minimum=0):
        fields={'checks':action['checks']+1,'next_check_at':self.clock()+self.delay(action,minimum)}
        if state:fields['state']=state
        if error:fields.update(failures=action['failures']+1,last_error=error[:1000])
        self.update(action,**fields)
        if error and action['failures']>=3:
            self.attention(action,'Repeated broker-verification failures; reads continue with backoff and no blind resubmission.')
        threshold=float(self.rules.get('recovery_escalate_hours',2))*3600
        if business_age(action['created_at'],self.clock())>=threshold:
            self.attention(action,'Broker action remains unresolved beyond the recovery age threshold; inspect the tracked evidence.')

    async def observe_close(self, trade, position, adapter):
        action=self.get(trade['id'],'close')
        if not action:return
        if position is None:
            self.finish(action,'CLOSED','A complete broker snapshot no longer contains the closing position; realized P/L awaits history.');return
        if not self.bind_position(action,trade,position):return
        if action['state']=='SUBMITTING':self.update(action,state='UNKNOWN')
        if action['next_check_at']>self.clock():return
        if action['state']=='RETRY_READY':
            try:await self.send(action,trade,position,adapter)
            except Exception:pass
            return
        if not action.get('order_id') and not action.get('request_id'):
            self.update(action,state='ESCALATED');self.attention(action,'Close outcome lacks a broker order ID or correlation reference; no duplicate close will be submitted.')
            self.schedule(action);return
        if action['state']=='ESCALATED' and action['evidence'].get('definitively_rejected'):
            self.schedule(action);return
        try:
            if action.get('order_id'):
                evidence=await asyncio.to_thread(adapter.lookup_close_order,action['order_id'])
            else:
                evidence=await asyncio.to_thread(adapter.lookup_order,reference_id=action['request_id'])
            if not isinstance(evidence,dict):raise ValueError('Close status unavailable')
            reported=evidence.get('orderID') or evidence.get('orderId')
            if action.get('order_id') and (reported is None or str(reported).removesuffix('.0')!=action['order_id']):raise ValueError('Close lookup did not confirm the expected order ID')
            if not action.get('order_id') and evidence.get('action') not in (None,'close'):
                raise ValueError('Correlation lookup did not identify a close order')
            if any(str(p.get('positionID')).removesuffix('.0')!=action['position_id'] for p in evidence.get('positions',[])):
                self.attention(action,'Close lookup reports a different position; automatic replacement is blocked.')
                raise ValueError('Close-order position identity mismatch')
            self.update(action,evidence=evidence)
            name=named_status(evidence)
            executed=[p for p in evidence.get('positions',[]) if str(p.get('positionID')).removesuffix('.0')==action['position_id'] and p.get('occurred') and p.get('rate')]
            failed=evidence.get('errorCode') not in (None,0,'0','')
            if name in {'rejected','cancelled','canceled','expired'} or (failed and not executed):
                if action['attempts']>=int(self.rules.get('recovery_max_attempts',3)):
                    self.update(action,state='ESCALATED');self.attention(action,'Close attempts exhausted after broker-confirmed terminal failure; position remains open.')
                    self.schedule(action)
                else:
                    # A replacement closes the remaining verified position only,
                    # after the prior order is authoritatively terminal.
                    self.schedule(action,state='RETRY_READY',minimum=900 if weekend(self.clock()) else 300)
            elif executed or name in {'filled','executed','completed'}:
                self.schedule(action,state='EXECUTION_REPORTED')
                if executed:self.attention(action,'Close execution was reported but the position is still present; verify partial execution/portfolio convergence before any replacement.')
            else:
                self.schedule(action,state='ACKNOWLEDGED' if action.get('order_id') else 'UNKNOWN')
                # Preserve numeric status IDs without guessing undocumented enums.
                if not action.get('order_id') and reported and evidence.get('action')=='close':
                    self.update(action,order_id=str(reported).removesuffix('.0'))
        except Exception as error:
            wait=float(getattr(error,'retry_after_seconds',None) or 0)
            self.schedule(action,error=f'Close status unavailable: {type(error).__name__}: {error}',minimum=wait)
            if getattr(error,'status_code',None) in {401,403}:self.attention(action,'Close-status access was denied; check broker credentials/permissions.')

    async def observe_protection(self, trade, position, adapter):
        action=self.get(trade['id'],'protection')
        if not action:return
        if position is None:self.finish(action,'CLOSED','Protection tracking resolved because the position is absent from a complete broker snapshot.');return
        if not self.bind_position(action,trade,position):return
        if self.adopt_tighter_protection(action,trade,position):return
        if self.protection_matches(action['payload'],position):
            if action['payload'].get('stop_loss_type')=='trailing':
                import native_trailing
                native_trailing.adopt_verified_stop(self.connect,trade,position)
                c=self.connect()
                try:
                    c.execute("UPDATE trades SET status=CASE WHEN status='RECONCILIATION_REQUIRED' THEN 'OPEN' ELSE status END,stop_state='broker_verified',reconciliation_status=CASE WHEN status='EXIT_PENDING' THEN reconciliation_status ELSE 'native_trailing_verified' END WHERE id=?",(trade['id'],));c.commit()
                finally:c.close()
            self.finish(action,'APPLIED','Broker-reported stop/target and requested stop type match the tracked protection intent.');return
        if action['state']=='SUBMITTING':self.update(action,state='UNKNOWN')
        if action['next_check_at']>self.clock():return
        if action['payload'].get('stop_loss_type')=='trailing':
            # Reapplying an uncertain native TSL can reset its trailing distance.
            # Continue reads and escalate; never infer rejection from a mismatch.
            self.schedule(action)
            if business_age(action['created_at'],self.clock())>=float(self.rules.get('recovery_escalate_hours',2))*3600:
                self.attention(action,'Native trailing protection remains unconfirmed; verification continues without resetting the stop.')
            return
        self.update(action,mismatches=action['mismatches']+1)
        if action['state']=='ESCALATED':self.schedule(action);return
        if action['attempts']>=int(self.rules.get('recovery_max_attempts',3)):
            self.update(action,state='ESCALATED');self.attention(action,'Protection still differs after bounded correction attempts; verification continues without further writes.');self.schedule(action);return
        elapsed=self.clock()-(action.get('last_attempt_at') or action['created_at'])
        if action['mismatches']<2 or elapsed<300:
            self.schedule(action);return
        try:
            rate=await asyncio.to_thread(adapter.get_rate,int(position['instrument_id']))
            side=trade['side'];bid=float(rate['bid']);ask=float(rate['ask'])
            if side not in {'LONG','SHORT'} or not math.isfinite(bid) or not math.isfinite(ask) or not 0<bid<=ask:
                raise ValueError('Fresh executable bid/ask is invalid')
            mark=bid if side=='LONG' else ask
            wanted=action['payload'];stop=float(wanted['stop_loss_price']);target=wanted.get('take_profit_price')
            valid=math.isfinite(mark) and mark>0 and (stop<mark and (target is None or mark<float(target)) if side=='LONG' else stop>mark and (target is None or mark>float(target)))
            actual=position.get('stop_loss_price')
            loosens=actual is not None and (float(actual)>stop+.01 if side=='LONG' else float(actual)<stop-.01)
            if not valid or loosens:
                self.update(action,state='ESCALATED');self.attention(action,'Protection correction needs review: price clearance failed or the broker stop is already tighter.');self.schedule(action);return
            self.update(action,state='RETRY_READY')
            # Absolute fixed-price protection is idempotent; reapply only the
            # identical intent after spaced mismatches, never move levels to chase price.
            await self.send(action,trade,position,adapter)
        except Exception as error:
            # Pre-transport failures must advance backoff even in RETRY_READY.
            # send() already schedules ambiguous transport outcomes.
            if action['next_check_at']<=self.clock():self.schedule(action,error=str(error))

    async def expire_pending_entry(self,trade,data,adapter):
        cancellation=self.get(trade['id'],'entry_cancel')
        executions=data.get('positionExecutions') or []
        state=named_status(data)
        if executions or state in {'filled','partiallyfilled','canceledpartiallyfilled','rejectedpartiallyfilled'}:
            if cancellation:self.finish(cancellation,'RESOLVED','Broker fill wins cancellation race; retain and protect the actual position.')
            return
        if state in {'rejected','cancelled','canceled','expired'}:
            if cancellation:self.finish(cancellation,'RESOLVED','Broker lookup confirms terminal entry outcome after cancellation request.')
            return
        submitted_at=json.loads(trade.get('policy_json') or '{}').get('entry_submitted_at')
        if not submitted_at:
            c=self.connect()
            try:r=c.execute("SELECT created_at FROM trade_events WHERE trade_id=? AND stage='execution' AND decision='submitted' ORDER BY id DESC LIMIT 1",(trade['id'],)).fetchone()
            finally:c.close()
            submitted_at=r['created_at'] if r else trade.get('created_at')
        age=self.clock()-epoch(submitted_at,self.clock())
        if age<float(self.rules.get('pending_entry_max_minutes',60))*60:return
        reported=str(data.get('orderId') or data.get('orderID') or '').removesuffix('.0')
        expected=str(trade.get('broker_order_id') or '').removesuffix('.0')
        if not expected or reported!=expected or not expected.isdigit():return
        if cancellation:return  # Never repeat an acknowledged or ambiguous cancel.
        if state not in {'received','placed','waitingformarket','pendingtriggeredrate'}:return
        cancel_request_id=str(uuid.uuid4())
        cancellation=self.create(trade,'entry_cancel',{'reason':'pending_entry_validity_expired','order_id':expected},state='SUBMITTING',order_id=expected,attempts=1,request_id=cancel_request_id)
        if cancellation.get('request_id')!=cancel_request_id:return
        # Persist before transport; a crash is not permission to send again.
        try:
            response=await asyncio.to_thread(adapter.cancel_pending_order,expected,request_id=cancellation['request_id'])
            self.update(cancellation,state='ACKNOWLEDGED',evidence=response)
            self.event(trade['id'],'entry_validity','position_manager','waiting','pending',
                'Expired pending entry: cancellation requested; broker confirmation or fill still required.',{'action_id':cancellation['id'],'order_id':expected})
        except Exception as error:
            self.update(cancellation,state='UNKNOWN',last_error=type(error).__name__)
            self.attention(cancellation,'Pending-entry cancellation unconfirmed; continue lookup without resubmission.')

    async def lookup_entry(self, trade, adapter):
        action=self.get(trade['id'],'entry') or self.create(trade,'entry',{'reference_id':trade.get('order_reference')},state='UNKNOWN',created_at=epoch(trade.get('created_at'),self.clock()),
            order_id=str(trade['broker_order_id']) if trade.get('broker_order_id') else None,request_id=None,stagger=False)
        if action['next_check_at']>self.clock():return None
        try:
            data=await asyncio.to_thread(adapter.lookup_order,order_id=trade.get('broker_order_id'),reference_id=trade.get('order_reference'))
            if not isinstance(data,dict):raise ValueError('Entry status unavailable')
            self.update(action,evidence=data)
            await self.expire_pending_entry(trade,data,adapter)
            if named_status(data) in {'rejected','cancelled','canceled','expired'}:
                self.finish(action,'RESOLVED','Broker lookup resolved the uncertain entry-order status.')
            elif len(data.get('positionExecutions') or [])==1:
                self.schedule(action,state='EXECUTION_REPORTED')
            else:self.schedule(action)
            return data
        except Exception as error:
            self.schedule(action,error=f'Entry lookup unavailable: {type(error).__name__}',minimum=float(getattr(error,'retry_after_seconds',None) or 0))
            if getattr(error,'status_code',None) in {401,403}:self.attention(action,'Entry-status access was denied; check broker credentials/permissions.')
            return None
