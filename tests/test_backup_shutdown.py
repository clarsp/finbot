import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import run_streamlit
from database_runtime import database_lock


class ShutdownTests(unittest.TestCase):
    def test_sigterm_stops_writers_before_final_backup(self):
        events=[]
        class Process:
            pid=123
            returncode=None
            def __init__(self,name):self.name=name
            def poll(self):return self.returncode
            def terminate(self):events.append(self.name);self.returncode=0
            def wait(self,timeout=None):
                if self.name=='backup':self.assert_timeout=timeout
                return 0
        processes=[]
        def spawn(command,**kwargs):
            name='scheduler' if 'makemoney.py' in command else 'backup' if 's3_backup.py' in command else 'frontend'
            process=Process(name);processes.append(process)
            if name=='frontend':signal.raise_signal(signal.SIGTERM)
            return process
        old=signal.getsignal(signal.SIGTERM)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db'
            settings=SimpleNamespace(database_path=path,enabled=True)
            with patch('run_streamlit.socket.socket'),patch('run_streamlit.BackupSettings.from_environment',return_value=settings),patch('run_streamlit.subprocess.Popen',side_effect=spawn),patch.dict(os.environ,{'FINBOT_LOG_TO_STDOUT':'true','ENABLE_HTTPS':'false'}):
                run_streamlit.main()
            with database_lock(path):pass
        self.assertEqual(events,['frontend','scheduler','backup'])
        self.assertEqual(processes[1].assert_timeout,90)
        self.assertEqual(signal.getsignal(signal.SIGTERM),old)
