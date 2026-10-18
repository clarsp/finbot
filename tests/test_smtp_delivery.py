from email.message import EmailMessage
from pathlib import Path
import smtplib
import unittest
from unittest.mock import MagicMock, patch
import email_notifications as mail


class SMTPDeliveryTests(unittest.TestCase):
    def send(self, refused=None, error=None, quit_error=None):
        settings=mail.SMTPSettings(True,'smtp.example',465,'tls','user','secret','from@example',('a@example','b@example'),20,'[Finbot]',True)
        smtp=MagicMock()
        smtp.send_message.return_value=refused or {}
        smtp.send_message.side_effect=error
        smtp.quit.side_effect=quit_error
        with patch.object(mail.SMTPSettings,'from_environment',return_value=settings),patch.object(mail,'load_trade_evidence',return_value={}),patch.object(mail,'build_trade_message',return_value=EmailMessage()),patch.object(mail.smtplib,'SMTP_SSL',return_value=smtp):
            result=mail.send_trade_execution_email(Path('unused'),1,test_mode=True)
        return result,smtp

    def test_partial_refusal_is_not_reported_as_all_sent(self):
        result,smtp=self.send({'b@example':(550,b'rejected')})
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['accepted_recipient_count'],1)
        self.assertEqual(result['refusal_codes'],[550])
        self.assertTrue(result['message_id'])
        smtp.sock.settimeout.assert_called_once_with(60)

    def test_quit_failure_cannot_retract_data_acceptance(self):
        result,smtp=self.send(quit_error=smtplib.SMTPServerDisconnected('closed'))
        self.assertEqual(result['delivery_state'],'smtp_accepted')
        smtp.send_message.assert_called_once()
        smtp.close.assert_called()

    def test_data_timeout_is_reported_without_automatic_resend(self):
        with self.assertRaises(TimeoutError):
            self.send(error=TimeoutError('DATA response unknown'))

    def test_all_recipients_refused_remains_failure(self):
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            self.send(error=smtplib.SMTPRecipientsRefused({'a@example':(550,b'rejected'),'b@example':(550,b'rejected')}))
