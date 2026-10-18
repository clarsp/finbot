import os
from pathlib import Path
import tempfile
import unittest
from scripts.export_fly_secrets import import_values, write_private, CREDENTIAL_KEYS

class FlyExportTests(unittest.TestCase):
    def fixture(self):
        credentials={key:'private-value' for key in CREDENTIAL_KEYS[:6]}
        credentials['FINBOT_SMTP_PASSWORD']='smtp-secret'
        credentials['UNRELATED_SECRET']='must-not-export'
        smtp=dict(enabled=True,host='mail.test',port=587,security='starttls',
            username='sender',sender='sender@example.test',recipients=['a@example.test','b@example.test'],
            attach_json=True,timeout_seconds=20,subject_prefix='[Finbot]')
        return credentials,smtp

    def test_exports_credentials_and_effective_smtp_only(self):
        values=import_values(*self.fixture())
        self.assertNotIn('UNRELATED_SECRET',values)
        self.assertNotIn('FINBOT_DB_PATH',values)
        self.assertEqual(values['FINBOT_TRADE_EMAIL_TO'],'a@example.test,b@example.test')
        self.assertEqual(values['FINBOT_TRADE_EMAIL_ENABLED'],'true')
        self.assertEqual(values['FINBOT_SMTP_PASSWORD'],'smtp-secret')

    def test_refuses_missing_credentials_and_multiline(self):
        credentials,smtp=self.fixture()
        del credentials['ETORO_USER_KEY']
        with self.assertRaises(ValueError):import_values(credentials,smtp)
        credentials,smtp=self.fixture();credentials['ETORO_USER_KEY']='a\nb'
        with self.assertRaises(ValueError):import_values(credentials,smtp)

    def test_private_atomic_file_and_quoted_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'secrets.env'
            write_private(path,{'PASSWORD':'a # b " c'})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(path.read_text(), 'PASSWORD=' + '"""' + 'a # b " c' + '"""' + '\n')
            self.assertEqual(list(Path(directory).iterdir()),[path])


    def test_ambiguous_import_value_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'secrets.env'
            path.write_text('previous export')
            with self.assertRaises(ValueError):
                write_private(path,{'PASSWORD':'quote"before#comment'})
            self.assertEqual(path.read_text(),'previous export')
            self.assertEqual(list(Path(directory).iterdir()),[path])
