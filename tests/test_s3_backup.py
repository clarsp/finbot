import io
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from datetime import datetime,timezone

from database_runtime import database_lock
from s3_backup import BackupSettings,S3SQLiteBackup,read_status,safe_error


class Missing(Exception):
    response={'Error':{'Code':'NoSuchKey'}}


class Storage:
    def __init__(self):self.objects={};self.fail=False;self.gets=0
    def upload_file(self,path,bucket,key,ExtraArgs):
        if self.fail:raise RuntimeError('SECRET must not appear')
        self.objects[key]=(Path(path).read_bytes(),ExtraArgs['Metadata'])
    def get_object(self,Bucket,Key):
        self.gets+=1
        if Key not in self.objects:raise Missing()
        data,metadata=self.objects[Key]
        return {'Body':io.BytesIO(data),'Metadata':metadata}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'db.sqlite'
        self.settings=BackupSettings(True,self.path,'datadb','finbot/test','auto','https://t3.storage.dev',900,'if_missing',False,'AES256',None,'tigris','key','secret')
        self.backup=S3SQLiteBackup(self.settings);self.storage=Storage();self.backup._client=self.storage
    def database(self,path=None):
        conn=sqlite3.connect(path or self.path)
        conn.execute('CREATE TABLE items(value)')
        conn.execute('INSERT INTO items VALUES (1)')
        conn.execute('CREATE TABLE app_settings(key TEXT PRIMARY KEY,value TEXT)')
        conn.execute("INSERT INTO app_settings VALUES ('demo_automated_trading_enabled','true')")
        conn.commit()
        return conn
    def test_wal_snapshot_and_isolated_verification(self):
        conn=self.database();conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('INSERT INTO items VALUES (2)');conn.commit()
        self.backup.create_snapshot()
        self.assertEqual(len(self.storage.objects),2)
        result=self.backup.verify_latest()
        self.assertEqual(result['sha256'],read_status(self.settings)['sha256'])
        destination=replace(self.settings,database_path=self.path.parent/'restored.sqlite')
        other=S3SQLiteBackup(destination);other._client=self.storage
        self.assertTrue(other.restore_latest())
        restored=sqlite3.connect(destination.database_path)
        self.assertEqual(restored.execute('SELECT count(*) FROM items').fetchone()[0],2)
        self.assertEqual(restored.execute("SELECT value FROM app_settings WHERE key='demo_automated_trading_enabled'").fetchone()[0],'false')
        restored.close();conn.close()
    def test_existing_wal_preserved_even_legacy_if_newer(self):
        conn=self.database();self.backup.create_snapshot()
        conn.execute('PRAGMA journal_mode=WAL');conn.execute('INSERT INTO items VALUES (2)');conn.commit()
        self.backup.settings=replace(self.settings,restore_mode='if_newer')
        self.assertFalse(self.backup.restore_latest())
        self.assertEqual(self.storage.gets,0)
        self.assertEqual(conn.execute('SELECT count(*) FROM items').fetchone()[0],2);conn.close()
    def test_corrupt_download_preserves_existing_database(self):
        conn=self.database();conn.close();original=self.path.read_bytes()
        self.backup.settings=replace(self.settings,restore_mode='always')
        self.storage.objects[self.settings.latest_key]=(b'broken',{'sha256':'wrong'})
        with self.assertRaises(RuntimeError):self.backup.restore_latest(allow_overwrite=True)
        self.assertEqual(self.path.read_bytes(),original)
    def test_checksum_valid_but_invalid_sqlite_rejected(self):
        data=b'not sqlite'
        self.storage.objects[self.settings.latest_key]=(data,{'sha256':hashlib.sha256(data).hexdigest()})
        with self.assertRaises(sqlite3.DatabaseError):self.backup.restore_latest()
        self.assertFalse(self.path.exists())
    def test_missing_metadata_rejected(self):
        conn=self.database();conn.close();self.backup.create_snapshot()
        data,_=self.storage.objects[self.settings.latest_key]
        self.storage.objects[self.settings.latest_key]=(data,{})
        with self.assertRaises(RuntimeError):self.backup.verify_latest()
    def test_missing_remote_policy(self):
        self.assertFalse(self.backup.restore_latest())
        self.backup.settings=replace(self.settings,restore_required=True)
        with self.assertRaises(Missing):self.backup.restore_latest()
    def test_explicit_overwrite_archives_old_database(self):
        conn=self.database();self.backup.create_snapshot()
        conn.execute('INSERT INTO items VALUES (2)');conn.commit();conn.close()
        self.backup.settings=replace(self.settings,restore_mode='always')
        with self.assertRaises(RuntimeError):self.backup.restore_latest()
        self.assertTrue(self.backup.restore_latest(allow_overwrite=True))
        archives=list((self.path.parent/'recovery').glob('*/db.sqlite'))
        self.assertEqual(len(archives),1)
        c=sqlite3.connect(archives[0]);self.assertEqual(c.execute('SELECT count(*) FROM items').fetchone()[0],2);c.close()
    def test_runtime_lock_blocks_restore(self):
        with database_lock(self.path):
            with self.assertRaises(RuntimeError):self.backup.restore_latest()
    def test_failed_upload_retains_latest_and_redacts_error(self):
        c=self.database();c.close();self.backup.create_snapshot()
        old=self.storage.objects[self.settings.latest_key];self.storage.fail=True
        with self.assertRaises(RuntimeError):self.backup.create_snapshot()
        self.assertEqual(self.storage.objects[self.settings.latest_key],old)
        self.assertEqual(read_status(self.settings)['last_error'],'RuntimeError')
        self.storage.fail=False;self.backup.create_snapshot()
        self.assertIsNone(read_status(self.settings)['last_error'])
    def test_status_is_bound_to_destination(self):
        c=self.database();c.close();self.backup.create_snapshot()
        self.assertEqual(read_status(replace(self.settings,prefix='other')),{})
    def test_tigris_config_and_env_precedence(self):
        config=self.path.parent/'env'
        config.write_text('FINBOT_S3_PROVIDER=tigris\nFINBOT_S3_BUCKET=file\nAWS_ACCESS_KEY_ID=private\nAWS_SECRET_ACCESS_KEY=secret\n')
        with patch.dict(os.environ,{'FINBOT_ENV_CONFIG_PATH':str(config),'FINBOT_S3_BUCKET':'host'},clear=True):
            settings=BackupSettings.from_environment()
        self.assertEqual(settings.bucket,'host');self.assertEqual(settings.region,'auto')
        self.assertEqual(settings.endpoint_url,'https://t3.storage.dev')
        self.assertNotIn('private',repr(settings));self.assertNotIn('secret',repr(settings))
    def test_iam_endpoint_rejected(self):
        with patch.dict(os.environ,{'FINBOT_ENV_CONFIG_PATH':'/nonexistent','FINBOT_S3_ENDPOINT_URL':'https://iam.storage.dev'},clear=True):
            with self.assertRaises(ValueError):BackupSettings.from_environment()
    def test_unique_snapshot_keys(self):
        now=datetime.now(timezone.utc)
        self.assertNotEqual(self.settings.versioned_key(now),self.settings.versioned_key(now))


    def test_wrapped_service_errors_expose_code_only(self):
        try:
            try:raise Missing('credential-bearing message')
            except Missing as error:raise RuntimeError('secret') from error
        except RuntimeError as error:
            self.assertEqual(safe_error(error),'RuntimeError: NoSuchKey')
