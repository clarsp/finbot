import asyncio
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
import smtplib
import unittest
from unittest.mock import Mock
import makemoney as app
import outcome_notifications as notifications
from email_notifications import SMTPSettings
import test_two_direction_workflow as fixtures

UTC=timezone.utc

class OutcomeNotificationTests(unittest.TestCase):
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update
    def setUp(self):
        fixtures.DatabaseTests.setUp(self)
        self.settings_for_broker=self.settings
        self.settings=SMTPSettings(True,'smtp.test',465,'tls','user','private','from@test',('one@test','two@test'),20,'[Finbot]',True)
    def tearDown(self):self.stack.close()
    def connect(self):return app.get_connection()
    def outcome(self,pid='p',net=10,closed='2026-10-18T12:00:00+00:00',fees=2):
        c=self.connect()
        c.execute('INSERT OR REPLACE INTO broker_closed_positions VALUES (?,?,?,?,?)',(pid,closed,net,fees,json.dumps({'close_rate':110,'units':3})))
        c.commit();c.close()
    def queue(self,pid='p'):
        c=self.connect()
        result=notifications.queue_closure(c,pid);c.commit();c.close()
        return result
    def states(self):
        c=self.connect()
        rows=[dict(r) for r in c.execute('SELECT * FROM outcome_notification_deliveries ORDER BY id')]
        c.close();return rows
    @staticmethod
    def accepted(settings,message,before_data):
        before_data()
        return {'status':'sent'}

    def test_unique_positions_net_totals_and_missing_pnl(self):
        for _ in range(2):
            tid=self.trade();self.update(tid,status='CLOSED',broker_position_id='p',realized_pnl=1000)
        self.outcome();self.outcome('loss',-4);self.outcome('flat',0);self.outcome('unknown',None)
        c=self.connect();result=notifications.report(c);c.close()
        self.assertEqual(result['summary']['closed_trades'],4)
        self.assertEqual(result['summary']['winning_gains'],10)
        self.assertEqual(result['summary']['losing_losses'],4)
        self.assertEqual(result['summary']['net_profit'],6)
        self.assertEqual(result['summary']['missing_net_outcomes'],1)
        self.assertEqual(result['summary']['break_even'],1)

    def test_existing_history_is_baselined_without_email(self):
        self.outcome()
        c=self.connect()
        c.execute("DELETE FROM app_settings WHERE key='outcome_notifications_started_at'")
        notifications.ensure_schema(c);c.commit();c.close()
        self.assertFalse(self.queue())
        send=Mock()
        notifications.process_outbox(self.connect,self.settings,send=send)
        send.assert_not_called()
        self.assertFalse(self.states())

    def test_closure_and_outbox_are_atomic(self):
        c=self.connect()
        c.execute("INSERT INTO broker_closed_positions VALUES ('p','2026-10-18T12:00:00Z',10,2,'{}')")
        self.assertTrue(notifications.queue_closure(c,'p'))
        c.rollback()
        self.assertFalse(c.execute('SELECT 1 FROM outcome_notification_events').fetchone())
        c.close()

    def test_duplicate_reconciliation_sends_once_per_recipient(self):
        self.outcome();self.assertTrue(self.queue());self.assertFalse(self.queue())
        messages=[]
        def send(settings,message,before_data):
            before_data();messages.append(message)
            self.assertTrue(any(r['state']=='SENDING' for r in self.states()))
            return {'status':'sent'}
        notifications.process_outbox(self.connect,self.settings,send=send)
        notifications.process_outbox(self.connect,self.settings,send=send)
        self.assertEqual(len(messages),2)
        self.assertTrue(all(r['state']=='SENT' for r in self.states()))
        self.assertEqual(len({m['Message-ID'] for m in messages}),2)
        self.assertTrue(all(m['Message-ID'].endswith('@test>') for m in messages))

    def test_recipient_failure_never_resends_accepted_recipient(self):
        self.outcome();self.queue();now=datetime.now(UTC);attempts=[]
        def send(settings,message,before_data):
            before_data();recipient=settings.recipients[0];attempts.append(recipient)
            if recipient=='two@test' and attempts.count(recipient)==1:
                raise smtplib.SMTPRecipientsRefused({recipient:(451,b'temporary')})
            return {'status':'sent'}
        notifications.process_outbox(self.connect,self.settings,now,send)
        self.assertEqual([r['state'] for r in self.states()],['SENT','RETRY'])
        notifications.process_outbox(self.connect,self.settings,now+timedelta(seconds=61),send)
        self.assertEqual(attempts,['one@test','two@test','two@test'])
        self.assertTrue(all(r['state']=='SENT' for r in self.states()))

    def test_data_timeout_is_uncertain_and_not_automatically_resent(self):
        self.outcome();self.queue()
        def send(settings,message,before_data):
            before_data();raise TimeoutError('PRIVATE RESPONSE')
        notifications.process_outbox(self.connect,self.settings,send=send)
        self.assertTrue(all(r['state']=='UNCERTAIN' for r in self.states()))
        self.assertTrue(all('PRIVATE' not in r['last_error'] for r in self.states()))
        followup=Mock()
        notifications.process_outbox(self.connect,self.settings,send=followup)
        followup.assert_not_called()
        with self.assertRaises(ValueError):app.retry_failed_outcome_notification(self.states()[0]['id'])

    def test_definitive_data_rejection_can_retry_without_duplicates(self):
        self.outcome();self.queue()
        def send(settings,message,before_data):
            before_data();raise smtplib.SMTPDataError(451,b'temporary')
        notifications.process_outbox(self.connect,self.settings,send=send)
        self.assertTrue(all(r['state']=='RETRY' for r in self.states()))

    def test_permanent_failure_requires_manual_retry(self):
        self.outcome();self.queue()
        def send(settings,message,before_data):
            raise smtplib.SMTPAuthenticationError(535,b'credentials PRIVATE')
        notifications.process_outbox(self.connect,self.settings,send=send)
        self.assertTrue(all(r['state']=='FAILED' for r in self.states()))
        app.retry_failed_outcome_notification(self.states()[0]['id'])
        self.assertEqual(self.states()[0]['state'],'PENDING')

    def test_retries_are_bounded(self):
        self.outcome();self.queue();now=datetime.now(UTC)
        def send(*args,**kwargs):raise TimeoutError('connect timeout')
        for step in range(5):
            notifications.process_outbox(self.connect,self.settings,now+timedelta(hours=step),send)
        self.assertTrue(all(r['state']=='FAILED' and r['attempts']==5 for r in self.states()))

    def test_crash_during_send_becomes_uncertain(self):
        self.outcome();self.queue();now=datetime.now(UTC)
        notifications.process_outbox(self.connect,replace(self.settings,enabled=False))
        c=self.connect()
        c.execute("INSERT INTO outcome_notification_deliveries(event_key,recipient,state,updated_at,message_id) VALUES ('closure:p','one@test','SENDING',?,'<stable@test>')",(now.timestamp()-601,))
        c.execute("UPDATE outcome_notification_events SET recipients_seeded=1")
        c.commit();c.close()
        send=Mock();notifications.process_outbox(self.connect,self.settings,now,send)
        send.assert_not_called();self.assertEqual(self.states()[0]['state'],'UNCERTAIN')

    def test_competing_dispatcher_does_not_send_same_recipient(self):
        self.outcome();self.queue();calls=[]
        def send(settings,message,before_data):
            before_data();calls.append(settings.recipients[0])
            notifications.process_outbox(self.connect,settings,send=Mock(side_effect=AssertionError('duplicate')))
            return {'status':'sent'}
        notifications.process_outbox(self.connect,replace(self.settings,recipients=('one@test',)),send=send)
        self.assertEqual(calls,['one@test'])

    def test_weekly_monday_schedule_boundaries_and_idempotence(self):
        self.outcome(net=12,closed='2026-10-18T21:59:59Z')
        self.outcome('next',500,closed='2026-10-18T22:00:00Z')
        c=self.connect()
        self.assertFalse(notifications.queue_weekly(c,datetime(2026,10,19,6,59,tzinfo=UTC)))
        self.assertTrue(notifications.queue_weekly(c,datetime(2026,10,19,7,tzinfo=UTC)))
        self.assertFalse(notifications.queue_weekly(c,datetime(2026,10,20,10,tzinfo=UTC)))
        c.commit()
        payload=json.loads(c.execute("SELECT payload FROM outcome_notification_events WHERE kind='weekly'").fetchone()[0]);c.close()
        self.assertEqual(payload['summary']['net_profit'],12)
        self.assertEqual(payload['summary']['closed_trades'],1)

    def test_dst_week_has_169_hours(self):
        c=self.connect()
        notifications.queue_weekly(c,datetime(2026,10,26,8,tzinfo=UTC));c.commit()
        payload=json.loads(c.execute("SELECT payload FROM outcome_notification_events WHERE kind='weekly'").fetchone()[0]);c.close()
        hours=(notifications.stamp(payload['window_end'])-notifications.stamp(payload['window_start'])).total_seconds()/3600
        self.assertEqual(hours,169)

    def test_empty_week_still_produces_report_with_csv(self):
        c=self.connect()
        notifications.queue_weekly(c,datetime(2026,10,19,8,tzinfo=UTC));c.commit()
        event=dict(c.execute("SELECT * FROM outcome_notification_events WHERE kind='weekly'").fetchone());c.close()
        message=notifications.build_message(self.settings,event,'<weekly@test>')
        self.assertIn('No broker-confirmed closed trades',message.get_body(('plain',)).get_content())
        self.assertIn('finbot-closed-trades.csv',[p.get_filename() for p in message.iter_attachments()])

    def test_unknown_pnl_email_does_not_fabricate_zero_profit(self):
        self.outcome(net=None);self.queue()
        c=self.connect();event=dict(c.execute('SELECT * FROM outcome_notification_events').fetchone());c.close()
        message=notifications.build_message(self.settings,event,'<closed@test>')
        self.assertIn('P/L unavailable',message['Subject'])
        self.assertNotIn('USD +0.00',message.get_body(('plain',)).get_content())

    def test_invalid_dates_excluded_and_html_csv_are_escaped(self):
        self.outcome('unknown',10,closed='bad-date')
        tid=self.trade();self.update(tid,status='CLOSED',symbol='<script>',broker_position_id='p');self.outcome()
        c=self.connect();result=notifications.report(c);self.queue()
        event=dict(c.execute('SELECT * FROM outcome_notification_events').fetchone());c.close()
        self.assertEqual(result['undated_history_excluded'],1)
        message=notifications.build_message(self.settings,event,'<closed@test>')
        self.assertNotIn('<script>',message.get_body(('html',)).get_content())
        csv=notifications.csv_report([{'symbol':'=HYPERLINK("bad")','net_profit':-4}])
        self.assertIn("'=HYPERLINK",csv)

    def test_disabled_closure_preference_suppresses_new_alerts(self):
        app.set_outcome_notification_preferences(False,True)
        self.outcome();self.queue()
        send=Mock()
        notifications.process_outbox(self.connect,self.settings,send=send)
        send.assert_not_called()
        app.set_outcome_notification_preferences(True,True)
        notifications.process_outbox(self.connect,self.settings,send=send)
        send.assert_not_called()

    def test_weekly_payload_is_frozen_once_queued(self):
        self.outcome(net=10)
        c=self.connect();notifications.queue_weekly(c,datetime(2026,10,19,8,tzinfo=UTC));c.commit();c.close()
        self.outcome(net=500)
        c=self.connect();self.assertFalse(notifications.queue_weekly(c,datetime(2026,10,20,8,tzinfo=UTC)))
        payload=json.loads(c.execute("SELECT payload FROM outcome_notification_events WHERE kind='weekly'").fetchone()[0]);c.close()
        self.assertEqual(payload['summary']['net_profit'],10)


    def test_production_weekly_requires_complete_post_window_history(self):
        now=datetime(2026,10,19,8,tzinfo=UTC)
        c=self.connect()
        self.assertFalse(notifications.queue_weekly(c,now,require_fresh_history=True))
        c.executemany("INSERT OR REPLACE INTO app_settings VALUES (?,?)",
            [('closed_history_synced_at','2026-10-18T21:59:00Z'),('closed_history_complete','true')])
        self.assertFalse(notifications.queue_weekly(c,now,require_fresh_history=True))
        c.execute("UPDATE app_settings SET value=? WHERE key='closed_history_synced_at'",(now.isoformat(),))
        c.execute("UPDATE app_settings SET value='false' WHERE key='closed_history_complete'")
        self.assertFalse(notifications.queue_weekly(c,now,require_fresh_history=True))
        c.execute("UPDATE app_settings SET value='true' WHERE key='closed_history_complete'")
        self.assertTrue(notifications.queue_weekly(c,now,require_fresh_history=True));c.close()

    def test_position_manager_queues_confirmed_closure_without_sending_inline(self):
        tid=self.trade();self.update(tid,status='OPEN',broker_position_id='p',broker_order_id='o')
        self.adapter.get_positions.return_value={'available':True,'positions':[]}
        self.adapter.get_closed_positions.return_value={'available':True,'complete':True,'positions':[
            {'position_id':'p','closed_at':'2026-10-18T12:00:00Z','net_profit':12,'fees':2,'close_rate':110,'units':3}]}
        asyncio.run(app.manage_etoro_demo_positions(self.settings_for_broker))
        c=self.connect()
        self.assertEqual(c.execute("SELECT count(*) FROM outcome_notification_events WHERE event_key='closure:p'").fetchone()[0],1)
        self.assertEqual(c.execute('SELECT count(*) FROM outcome_notification_deliveries').fetchone()[0],0)
        self.assertEqual(self.row(tid)['status'],'CLOSED')
        c.close()


    def test_closure_without_broker_timestamp_still_alerts_without_inventing_date(self):
        self.outcome(closed=None)
        self.assertTrue(self.queue())
        c=self.connect()
        event=dict(c.execute('SELECT * FROM outcome_notification_events').fetchone())
        report=notifications.report(c);c.close()
        message=notifications.build_message(self.settings,event,'<missing-time@test>')
        self.assertIn('broker timestamp missing',message.get_body(('plain',)).get_content())
        self.assertEqual(report['summary']['closed_trades'],0)
        self.assertEqual(report['undated_history_excluded'],1)

class ClosureReasonTests(unittest.TestCase):
    setUp=OutcomeNotificationTests.setUp
    tearDown=OutcomeNotificationTests.tearDown
    trade=OutcomeNotificationTests.trade
    row=OutcomeNotificationTests.row
    update=OutcomeNotificationTests.update
    connect=OutcomeNotificationTests.connect
    outcome=OutcomeNotificationTests.outcome
    queue=OutcomeNotificationTests.queue
    def test_trailing_reason_comes_from_matching_submitted_action(self):
        tid=self.trade();self.update(tid,broker_position_id='p',status='CLOSED')
        self.outcome()
        c=self.connect()
        payload={'reason':'profit_trailing:drawdown','profit_trailing':{'tier':'prime','peak_profit_usd':10.67,'profit_usd':5,'drawdown_usd':5.67,'trigger_profit_usd':7.67}}
        c.execute("INSERT INTO broker_actions(trade_id,position_id,kind,state,created_at,updated_at,next_check_at,attempts,order_id,last_attempt_at,payload) VALUES (?,?,'close','CLOSED',1,1,1,1,'close-id',1,?)",(tid,'p',json.dumps(payload)));c.commit()
        row=notifications.closed_outcomes(c)[0][0];c.close()
        self.assertEqual(row['closure_reason'],'Profit trailing exit')
        self.assertEqual(row['closure_reason_basis'],'finbot_request')
        self.assertIn('10.67',row['closure_reason_detail'])
        self.queue()
        c=self.connect();event=dict(c.execute("SELECT * FROM outcome_notification_events WHERE kind='closure'").fetchone());c.close()
        message=notifications.build_message(self.settings,event,'<reason@test>')
        self.assertIn('Closure reason: Profit trailing exit',message.get_body(preferencelist=('plain',)).get_content())

    def test_atr_trailing_reason_reports_price_trigger_and_retreat(self):
        tid=self.trade();self.update(tid,broker_position_id='p',status='CLOSED');self.outcome()
        evidence={'basis':'entry_atr_v1','side':'SHORT','tier':'major','entry_price':100,'entry_atr':10,'best_price':85,'current_price':88,'retreat_atr':.3,'allowed_retreat_atr':.25,'trigger_price':87.5,'profit_usd':12}
        c=self.connect()
        c.execute("INSERT INTO broker_actions(trade_id,position_id,kind,state,created_at,updated_at,next_check_at,attempts,order_id,last_attempt_at,payload) VALUES (?,?,'close','CLOSED',1,1,1,1,'close-id',1,?)",(tid,'p',json.dumps({'reason':'profit_trailing:drawdown','profit_trailing':evidence})));c.commit()
        row=notifications.closed_outcomes(c)[0][0];c.close()
        self.assertIn('Major ATR tier',row['closure_reason_detail'])
        self.assertIn('trigger price 87.5',row['closure_reason_detail'])
        self.assertIn('retreat 0.3 ATR',row['closure_reason_detail'])

    def test_broker_reason_overrides_price_and_unknown_numeric_code(self):
        self.outcome()
        c=self.connect()
        c.execute('UPDATE broker_closed_positions SET payload=?',(json.dumps({'raw':{'closeReason':'Take Profit'}}),));c.commit()
        row=notifications.closed_outcomes(c)[0][0]
        self.assertEqual(row['closure_reason'],'Take-profit');self.assertEqual(row['closure_reason_basis'],'broker_reported')
        c.execute('UPDATE broker_closed_positions SET payload=?',(json.dumps({'raw':{'closeReason':7},'close_rate':110}),));c.commit()
        self.assertEqual(notifications.closed_outcomes(c)[0][0]['closure_reason'],'Unknown');c.close()

    def test_unsubmitted_or_superseded_request_is_not_a_closure_reason(self):
        tid=self.trade();self.update(tid,broker_position_id='p',status='CLOSED');self.outcome()
        c=self.connect()
        c.execute("INSERT INTO broker_actions(trade_id,position_id,kind,state,created_at,updated_at,next_check_at,attempts,order_id,last_attempt_at,payload) VALUES (?,?,'close','SUPERSEDED',1,1,1,1,'close-id',1,?)",(tid,'p',json.dumps({'reason':'stop_level_observed'})));c.commit()
        self.assertEqual(notifications.closed_outcomes(c)[0][0]['closure_reason'],'Unknown');c.close()
