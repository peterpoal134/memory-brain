"""Deterministic role-signal rules; no conversation classifier or model calls."""
import copy
import json
import math
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from xiaojiu_brain.extensions import EMOTION_AXES, EMOTION_PRESETS, Extensions


class EmotionRuleTests(unittest.TestCase):
    NOW=1_800_000_000.

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'preferences.json'
        with patch('xiaojiu_brain.extensions.time.time',return_value=self.NOW):self.ext=Extensions(self.path)

    def tearDown(self):self.tmp.cleanup()

    def at(self,stamp,method,*args,**kwargs):
        with patch('xiaojiu_brain.extensions.time.time',return_value=stamp):return method(*args,**kwargs)

    def test_new_install_axis_defaults_and_legacy_curve_migration_preserve_raw_anchor(self):
        rules=self.ext.emotion_rules()
        self.assertEqual(rules['half_life_hours'],{'pleasure':12,'arousal':2,'dominance':6})
        old={'settings':{'emotion_half_life_hours':5},'self':{'notes':[],'style_preferences':''},
             'emotion':{'pleasure':.7,'arousal':-.4,'dominance':.2,'notes':'Synthetic owner note.',
                        'tags':['synthetic'],'updated_at':self.NOW}}
        self.path.write_text(json.dumps(old),encoding='utf8')
        migrated=self.at(self.NOW+3600,Extensions,self.path)
        self.assertEqual(migrated.emotion_rules()['half_life_hours'],dict.fromkeys(EMOTION_AXES,5))
        self.assertEqual(json.loads(self.path.read_text())['emotion'],old['emotion'])
        projected=self.at(self.NOW+5*3600,migrated.emotion)
        for axis in EMOTION_AXES:self.assertAlmostEqual(projected[axis],old['emotion'][axis]/2,places=4)
        restarted=self.at(self.NOW+6*3600,Extensions,self.path)
        self.assertEqual(restarted.emotion_rules()['half_life_hours'],dict.fromkeys(EMOTION_AXES,5))

    def test_axis_projection_is_read_only_and_reports_its_actual_explanation(self):
        self.at(self.NOW,self.ext.update_emotion,{'pleasure':.8,'arousal':.8,'dominance':.8,'notes':'synthetic','tags':['owner']})
        before=self.path.read_bytes();result=self.at(self.NOW+6*3600,self.ext.emotion)
        self.assertAlmostEqual(result['pleasure'],.8*2**(-.5),places=4)
        self.assertEqual(result['arousal'],.1);self.assertEqual(result['dominance'],.4)
        self.assertTrue(result['signal_only']);self.assertTrue(result['rule_simulation'])
        trace=result['trace']['axes']['arousal']
        self.assertEqual(trace['half_life_hours'],2);self.assertEqual(trace['baseline'],0)
        self.assertEqual(trace['anchor'],.8);self.assertEqual(trace['elapsed_hours'],6)
        self.assertEqual(trace['decay_factor'],.125);self.assertEqual(trace['value'],result['arousal'])
        self.at(self.NOW+7*3600,self.ext.emotion);self.ext.emotion_rules();self.ext.emotion_events()
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(self.ext.emotion_events()['total'],0)

    def test_changing_a_half_life_reanchors_current_values_without_a_retroactive_jump(self):
        self.at(self.NOW,self.ext.update_emotion,dict.fromkeys(EMOTION_AXES,.8))
        stamp=self.NOW+2*3600;before=self.at(stamp,self.ext.emotion)
        self.at(stamp,self.ext.update_settings,{'emotion_pleasure_half_life_hours':1})
        immediate=self.at(stamp,self.ext.emotion)
        for axis in EMOTION_AXES:self.assertEqual(immediate[axis],before[axis])
        after=self.at(stamp+3600,self.ext.emotion)
        self.assertAlmostEqual(after['pleasure'],before['pleasure']/2,places=4)
        self.assertAlmostEqual(after['arousal'],before['arousal']*2**(-.5),places=4)
        self.assertAlmostEqual(after['dominance'],before['dominance']*2**(-1/6),places=4)
        self.assertEqual(json.loads(self.path.read_text())['emotion']['updated_at'],stamp)

    def test_shared_half_life_updates_all_axes_unless_explicitly_overridden_in_same_patch(self):
        self.at(self.NOW,self.ext.update_emotion,{'pleasure':.6,'arousal':-.4,'dominance':.2})
        stamp=self.NOW+3600;before=self.at(stamp,self.ext.emotion)
        self.at(stamp,self.ext.update_settings,{'emotion_half_life_hours':4,'emotion_dominance_half_life_hours':9})
        self.assertEqual(self.ext.emotion_rules()['half_life_hours'],{'pleasure':4,'arousal':4,'dominance':9})
        after=self.at(stamp,self.ext.emotion)
        for axis in EMOTION_AXES:self.assertEqual(after[axis],before[axis])

    def test_unrelated_settings_preserve_existing_emotion_anchor_and_notes(self):
        self.at(self.NOW,self.ext.update_emotion,{'pleasure':.7,'notes':'Keep original note.','tags':['keep']})
        before=copy.deepcopy(self.ext.data['emotion'])
        self.at(self.NOW+10000,self.ext.update_settings,{'token_budget':2400,'life_enabled':True})
        self.assertEqual(self.ext.data['emotion'],before)
        self.assertEqual(json.loads(self.path.read_text())['emotion'],before)

    def test_presets_use_directional_saturation_and_intensity_without_keyword_classification(self):
        for index,(kind,preset) in enumerate(EMOTION_PRESETS.items()):
            ext=self.at(self.NOW,Extensions,self.path.parent/(kind+'.json'))
            result=self.at(self.NOW,ext.apply_emotion_event,{'kind':kind,'intensity':.5,'idempotency_key':kind})
            self.assertTrue(result['applied']);self.assertEqual(result['event']['origin'],'owner_manual_event')
            for axis in EMOTION_AXES:self.assertAlmostEqual(result['emotion'][axis],preset['delta'][axis]*.5,places=4)
        self.at(self.NOW,self.ext.update_emotion,{'pleasure':1.,'arousal':1.,'dominance':1.})
        high=self.at(self.NOW,self.ext.apply_emotion_event,{'kind':'joy','intensity':1})
        self.assertEqual({axis:high['emotion'][axis] for axis in EMOTION_AXES},dict.fromkeys(EMOTION_AXES,1.))
        self.at(self.NOW+1,self.ext.update_emotion,dict.fromkeys(EMOTION_AXES,-1.))
        low=self.at(self.NOW+1,self.ext.apply_emotion_event,{'kind':'sadness','intensity':1})
        for axis in EMOTION_AXES:self.assertEqual(low['emotion'][axis],-1.)

    def test_cooldown_idempotent_restart_and_conflicting_key_do_not_reapply_impulses(self):
        request={'kind':'excitement','intensity':.7,'idempotency_key':'stable-synthetic-key'}
        accepted=self.at(self.NOW,self.ext.apply_emotion_event,request)
        before=self.path.read_bytes()
        blocked=self.at(self.NOW+299,self.ext.apply_emotion_event,{**request,'idempotency_key':'different-key'})
        self.assertFalse(blocked['applied']);self.assertEqual(blocked['reason'],'cooldown');self.assertEqual(self.path.read_bytes(),before)
        replay=self.at(self.NOW+1000,self.ext.apply_emotion_event,request)
        self.assertFalse(replay['applied']);self.assertTrue(replay['replayed']);self.assertEqual(replay['event']['id'],accepted['event']['id'])
        self.assertEqual(self.path.read_bytes(),before)
        restarted=self.at(self.NOW+1000,Extensions,self.path);saved=self.path.read_bytes()
        self.assertTrue(self.at(self.NOW+2000,restarted.apply_emotion_event,request)['replayed'])
        with self.assertRaises(ValueError):self.at(self.NOW+2000,restarted.apply_emotion_event,{**request,'intensity':.4})
        self.assertEqual(self.path.read_bytes(),saved)
        renewed=self.at(self.NOW+2000,restarted.apply_emotion_event,{**request,'idempotency_key':'new-key'})
        self.assertTrue(renewed['applied'])

    def test_ledger_is_bounded_and_projection_does_not_store_trace_in_legacy_anchor(self):
        for index in range(103):
            result=self.at(self.NOW+301*index,self.ext.apply_emotion_event,{'kind':'joy','idempotency_key':str(index)})
            self.assertTrue(result['applied'])
            for axis in EMOTION_AXES:self.assertTrue(-1<=result['emotion'][axis]<=1)
        events=self.ext.emotion_events(100)
        self.assertEqual(events['total'],100);self.assertEqual(len(events['items']),100)
        self.assertEqual(events['items'][0]['idempotency_key'],'102');self.assertEqual(events['items'][-1]['idempotency_key'],'3')
        raw=json.loads(self.path.read_text())['emotion']
        self.assertEqual(set(raw),{'pleasure','arousal','dominance','tags','notes','updated_at'})
        restarted=self.at(self.NOW+301*103,Extensions,self.path)
        self.assertEqual(restarted.emotion_events(100),events)

    def test_invalid_numeric_fields_and_event_data_reject_atomically(self):
        invalid=[True,False,float('nan'),float('inf'),-float('inf'),'1',None]
        for value in invalid:
            for axis in EMOTION_AXES:
                before=self.path.read_bytes()
                with self.assertRaises(ValueError):self.ext.update_emotion({axis:value})
                self.assertEqual(self.path.read_bytes(),before)
            for key in ['emotion_half_life_hours',*(f'emotion_{axis}_half_life_hours' for axis in EMOTION_AXES)]:
                before=self.path.read_bytes()
                with self.assertRaises(ValueError):self.ext.update_settings({key:value,'life_enabled':True})
                self.assertEqual(self.path.read_bytes(),before)
        bad_events=[{'kind':'joy','intensity':value} for value in invalid]
        bad_events.extend([{'kind':'unknown'},{'kind':True},{'kind':'joy','text':'Do not classify this conversation.'},
                           {'kind':'joy','intensity':1.1},{'kind':'joy','idempotency_key':''},
                           {'kind':'joy','idempotency_key':False},{'kind':'joy','idempotency_key':'bad\x00key'}])
        for request in bad_events:
            before=self.path.read_bytes()
            with self.assertRaises(ValueError):self.ext.apply_emotion_event(request)
            self.assertEqual(self.path.read_bytes(),before)
        for limit in [True,0,101,1.5]:
            with self.assertRaises(ValueError):self.ext.emotion_events(limit)

    def test_failed_persistence_leaves_anchor_and_event_ledger_unchanged(self):
        before=copy.deepcopy(self.ext.data);disk=self.path.read_bytes()
        with patch('xiaojiu_brain.extensions.os.replace',side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):self.at(self.NOW,self.ext.apply_emotion_event,{'kind':'comfort'})
        self.assertEqual(self.ext.data,before);self.assertEqual(self.path.read_bytes(),disk)

    def test_concurrent_replay_has_one_atomic_event_and_no_duplicate_change(self):
        barrier=threading.Barrier(2);results=[];errors=[]
        def run():
            try:
                barrier.wait();results.append(self.ext.apply_emotion_event({'kind':'rest','idempotency_key':'concurrent'}))
            except Exception as error:errors.append(error)
        with patch('xiaojiu_brain.extensions.time.time',return_value=self.NOW):
            threads=[threading.Thread(target=run) for _ in range(2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(2)
        self.assertEqual(errors,[]);self.assertEqual(len(results),2)
        self.assertEqual(sum(item['applied'] for item in results),1);self.assertEqual(sum(item['replayed'] for item in results),1)
        self.assertEqual(self.ext.emotion_events()['total'],1)

    def test_backward_clock_does_not_amplify_projection_or_bypass_cooldown(self):
        event=self.at(self.NOW,self.ext.apply_emotion_event,{'kind':'stress','intensity':.5})
        backward=self.at(self.NOW-3600,self.ext.emotion)
        for axis in EMOTION_AXES:self.assertEqual(backward[axis],event['emotion'][axis])
        blocked=self.at(self.NOW-3600,self.ext.apply_emotion_event,{'kind':'stress'})
        self.assertEqual(blocked['reason'],'cooldown')


if __name__=='__main__':unittest.main()
