"""Finite configuration deadlines; no provider or filesystem operations."""
from pathlib import Path
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from xiaojiu_brain.config import Config

class TimeoutConfigTests(unittest.TestCase):
    def test_invalid_timeouts_fail_before_service_start(self):
        for value in (True,False,None,'45',0,-1,float('inf'),float('-inf'),float('nan'),601):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    Config(api_token='synthetic-config-test-token-00000000',model_timeout=value).validate()
    def test_normal_and_short_test_deadlines_remain_valid(self):
        for value in (.01,1,45,600):
            with self.subTest(value=value):
                Config(api_token='synthetic-config-test-token-00000000',model_timeout=value).validate()

if __name__=='__main__':unittest.main(verbosity=2)
