"""Local certificates must validate localhost and survive container restarts."""
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
from run_streamlit import generate_self_signed_cert

class LocalTLSTests(unittest.TestCase):
    def test_localhost_sans_private_key_permissions_and_certificate_reuse(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'FINBOT_TLS_CERT_DIR':directory}):
            certificate,key=generate_self_signed_cert()
            decoded=ssl._ssl._test_decode_cert(certificate)
            self.assertIn(('DNS','localhost'),decoded['subjectAltName'])
            self.assertIn(('IP Address','127.0.0.1'),decoded['subjectAltName'])
            self.assertEqual(Path(key).stat().st_mode & 0o777,0o600)
            before=Path(certificate).read_bytes()
            self.assertEqual(generate_self_signed_cert(),(certificate,key))
            self.assertEqual(Path(certificate).read_bytes(),before)
            ssl.create_default_context(cafile=certificate).load_cert_chain(certificate,key)
