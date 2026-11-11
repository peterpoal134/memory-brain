"""Settings compatibility and bounds; uses temporary files and no model/service."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from xiaojiu_brain.config import Config
from xiaojiu_brain.extensions import DEFAULT_SETTINGS, Extensions


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.path=self.root/'preferences.json'
    def tearDown(self):self.temp.cleanup()

    def test_old_preferences_gain_defaults_without_losing_existing_owner_state(self):
        old={'settings':{'derive_interval':17,'derive_enabled':False,'cross_conversation':True,'token_budget':2400},
            'self':{'notes':[{'id':'original','text':'合成角色笔记','created_at':10}],'style_preferences':'简洁'},
            'emotion':{'pleasure':.4,'arousal':.2,'dominance':0,'tags':['合成'],'notes':'保留','updated_at':10}}
        self.path.write_text(json.dumps(old,ensure_ascii=False),encoding='utf8')
        ext=Extensions(self.path);settings=ext.settings()
        self.assertEqual(settings['derive_batch_messages'],12)
        self.assertEqual(settings['derive_max_wait_seconds'],900)
        for k,v in old['settings'].items():self.assertEqual(settings[k],v)
        saved=json.loads(self.path.read_text(encoding='utf8'))
        self.assertEqual(saved['self'],old['self']);self.assertEqual(saved['emotion'],old['emotion'])
        self.assertEqual(saved['settings'],settings)
        self.assertEqual(Extensions(self.path).settings(),settings)

    def test_strict_integer_batch_and_wait_ranges_and_atomic_rejection(self):
        ext=Extensions(self.path)
        for batch,wait in ((1,30),(12,86400),(6,900)):
            actual=ext.update_settings({'derive_batch_messages':batch,'derive_max_wait_seconds':wait})
            self.assertEqual((actual['derive_batch_messages'],actual['derive_max_wait_seconds']),(batch,wait))
        invalid={'derive_batch_messages':[0,13,-1,1.0,'12',True,None,float('nan')],
            'derive_max_wait_seconds':[29,86401,-1,900.0,'900',False,None,float('inf')]}
        for key,values in invalid.items():
            for value in values:
                with self.subTest(key=key,value=value):
                    before=self.path.read_bytes();state=ext.settings()
                    with self.assertRaises(ValueError):ext.update_settings({key:value,'derive_enabled':False})
                    self.assertEqual(ext.settings(),state);self.assertEqual(self.path.read_bytes(),before)

    def test_existing_check_interval_range_keeps_fractional_values_but_rejects_nonfinite_or_boolean(self):
        ext=Extensions(self.path)
        for value in (1,1.5,600):self.assertEqual(ext.update_settings({'derive_interval':value})['derive_interval'],value)
        for value in (0,600.1,float('nan'),float('inf'),True,'5'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):ext.update_settings({'derive_interval':value})

    def test_old_configuration_loads_new_defaults_and_explicit_values_round_trip(self):
        path=self.root/'config.json';old=asdict(Config(api_token='synthetic-local-token-0123456789'))
        old.pop('derive_batch_messages');old.pop('derive_max_wait_seconds');old['derive_interval']=12
        path.write_text(json.dumps(old),encoding='utf8')
        cfg=Config.load(path)
        self.assertEqual((cfg.derive_batch_messages,cfg.derive_max_wait_seconds,cfg.derive_interval),(12,900,12))
        newpath=self.root/'new-config.json'
        Config.initialize(newpath,derive_batch_messages=4,derive_max_wait_seconds=600)
        explicit=Config.load(newpath)
        self.assertEqual((explicit.derive_batch_messages,explicit.derive_max_wait_seconds),(4,600))

    def test_configuration_rejects_bad_new_bounds_before_use(self):
        for field,values in {'derive_batch_messages':[0,13,6.0,True,'6'],
            'derive_max_wait_seconds':[29,86401,900.0,False,'900'],
            'derive_interval':[0,601,float('nan'),float('inf'),True]}.items():
            for value in values:
                with self.subTest(field=field,value=value):
                    cfg=Config(api_token='synthetic-local-token-0123456789',**{field:value})
                    with self.assertRaises(ValueError):cfg.validate()

    def test_preferences_preserve_nondefault_batch_settings_after_restart(self):
        ext=Extensions(self.path);ext.update_settings({'derive_batch_messages':5,'derive_max_wait_seconds':1200})
        loaded=Extensions(self.path,defaults={'derive_batch_messages':2,'derive_max_wait_seconds':60})
        self.assertEqual((loaded.settings()['derive_batch_messages'],loaded.settings()['derive_max_wait_seconds']),(5,1200))
        self.assertEqual((DEFAULT_SETTINGS['derive_batch_messages'],DEFAULT_SETTINGS['derive_max_wait_seconds']),(12,900))

    def test_life_defaults_migrate_and_catalog_reports_implemented_enabled_state(self):
        self.path.write_text(json.dumps({'settings':{'derive_interval':19,'token_budget':2400},
            'self':{'style_preferences':'保留原偏好','notes':[]}}),encoding='utf8')
        ext=Extensions(self.path);s=ext.settings()
        self.assertEqual((s['life_enabled'],s['life_context_enabled'],s['life_interval_seconds']),(False,True,1200))
        self.assertEqual((s['life_wake_time'],s['life_sleep_time']),('08:00','23:30'))
        self.assertEqual(s['derive_interval'],19)
        self.assertEqual(ext.self_notes()['style_preferences'],'保留原偏好')
        item=next(item for item in ext.catalog()['items'] if item['id']=='life')
        self.assertTrue(item['implemented']);self.assertFalse(item['enabled'])
        self.assertEqual(item['setting_key'],'life_enabled')
        ext.update_settings({'life_enabled':True,'life_context_enabled':False})
        again=Extensions(self.path)
        self.assertTrue(next(item for item in again.catalog()['items'] if item['id']=='life')['enabled'])
        self.assertFalse(again.settings()['life_context_enabled'])

    def test_life_interval_boolean_and_strict_clock_validation(self):
        ext=Extensions(self.path)
        for value in (300,86400,1200):self.assertEqual(ext.update_settings({'life_interval_seconds':value})['life_interval_seconds'],value)
        invalid={'life_interval_seconds':[299,86401,True,300.0,'1200',float('inf'),float('nan')],
            'life_enabled':[0,1,'true'], 'life_context_enabled':[0,1,'false'],
            'life_wake_time':['8:00','08:0','24:00','12:60','08:00:00',' 08:00','０８:００',None,True],
            'life_sleep_time':['23:6','23:60','23：30','00:00Z',17]}
        for key,values in invalid.items():
            for value in values:
                with self.subTest(key=key,value=value):
                    saved=self.path.read_bytes();state=ext.settings()
                    with self.assertRaises(ValueError):ext.update_settings({key:value})
                    self.assertEqual(ext.settings(),state);self.assertEqual(self.path.read_bytes(),saved)

    def test_life_wake_sleep_validation_is_atomic_over_the_merged_candidate(self):
        ext=Extensions(self.path);before=self.path.read_bytes();state=ext.settings()
        for patch in ({'life_wake_time':'23:30','life_enabled':True},
                      {'life_wake_time':'07:00','life_sleep_time':'07:00','life_interval_seconds':300}):
            with self.assertRaises(ValueError):ext.update_settings(patch)
            self.assertEqual(ext.settings(),state);self.assertEqual(self.path.read_bytes(),before)
        swapped=ext.update_settings({'life_wake_time':'23:30','life_sleep_time':'08:00'})
        self.assertEqual((swapped['life_wake_time'],swapped['life_sleep_time']),('23:30','08:00'))


if __name__=='__main__':unittest.main()
