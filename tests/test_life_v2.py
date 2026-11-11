"""Source-backed bounded life curves; no model, network or private-content reads."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from xiaojiu_brain.life import LifeLayer
from xiaojiu_brain.storage import Store


def epoch(value):
    return dt.datetime.fromisoformat(value).timestamp()


class BoundedLifeTests(unittest.TestCase):
    WAKE=epoch('2026-05-02T08:00:00+00:00')
    NOON=epoch('2026-05-02T14:00:00+00:00')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'brain.db'
        self.store=Store(self.path)
        self.settings={'life_enabled':True,'life_context_enabled':True,
                       'life_wake_time':'08:00','life_sleep_time':'20:00'}
        self.life=LifeLayer(self.store,'owner','UTC',lambda:dict(self.settings))
        self.life.tick(self.WAKE)

    def tearDown(self):
        self.life.stop();self.store.close();self.tmp.cleanup()

    def state(self,at=None):
        return self.life.snapshot(self.NOON if at is None else at)

    def chat(self,ident,at,role='user',kind='user',conversation='chat',owner='owner'):
        raw=self.store.ingest(owner,conversation,ident,role,'合成源正文不应出现在曲线或规则',
                              recorded_at=at,metadata={'source_kind':kind})
        self.life.note_chat(raw['id'],conversation)
        return raw

    def test_day_rest_bases_are_smooth_and_idle_does_not_saturate(self):
        self.assertAlmostEqual(self.state(self.WAKE)['needs']['energy'],.85)
        sleep=self.WAKE+12*3600
        self.assertAlmostEqual(self.state(sleep)['needs']['energy'],.35)
        for boundary in (self.WAKE,sleep,self.WAKE+86400):
            before=self.state(boundary-.1)['needs'];after=self.state(boundary+.1)['needs']
            for key in before:self.assertLess(abs(before[key]-after[key]),.0002)
        day=self.state()['needs'];night=self.state(self.WAKE+18*3600)['needs']
        for key in ('boredom','social','curiosity'):self.assertLess(night[key],day[key])
        remote=self.state(self.NOON+36500*86400)['needs']
        self.assertEqual(remote,day)
        self.assertLess(max(remote.values()),1)

    def test_original_user_chat_impulse_relaxes_instead_of_resetting_clock(self):
        idle=self.state();self.chat('one',self.NOON)
        active=self.state()
        effect=active['needs_trace']['activity_correction']
        self.assertEqual(effect,{'energy':-.015,'boredom':-.08,'social':-.1,'curiosity':.02})
        self.assertLess(active['needs']['social'],idle['needs']['social'])
        later=self.state(self.NOON+7200)['needs_trace']['activity_correction']
        for key in effect:self.assertAlmostEqual(later[key],effect[key]/2,places=6)
        settled=self.state(self.NOON+49*3600)['needs_trace']
        self.assertTrue(all(value==0 for value in settled['activity_correction'].values()))

    def test_chat_bucket_uses_earliest_global_valid_source_and_no_assistant_import_effect(self):
        first=self.chat('first',self.NOON)
        baseline=self.state(self.NOON+120)['needs_trace']
        self.chat('same-bucket',self.NOON+60,conversation='another')
        self.chat('model',self.NOON+80,role='assistant',kind='model')
        self.chat('import',self.NOON+90,kind='imported_raw')
        self.chat('note',self.NOON+95,kind='owner_note')
        current=self.state(self.NOON+120)['needs_trace']
        self.assertEqual(current['eligible_activity_counts']['chat'],1)
        self.assertEqual(current['activity_correction'],baseline['activity_correction'])
        self.store.delete_message('owner',first['id'])
        self.assertEqual(self.state(self.NOON+120)['needs_trace']['eligible_activity_counts']['chat'],1)
        # Removing all valid user sources removes the pulse, not just its display.
        other=self.store.find_message('owner','another','same-bucket')
        self.store.delete_message('owner',other['id'])
        self.assertEqual(self.state(self.NOON+120)['needs_trace']['eligible_activity_counts']['chat'],0)

    def test_future_pre_activation_and_foreign_sources_do_not_drive_signals(self):
        self.chat('old',self.WAKE-1)
        self.chat('future',self.NOON+1)
        other=LifeLayer(self.store,'foreign','UTC',lambda:dict(self.settings))
        try:
            raw=self.store.ingest('foreign','chat','other','user','OTHER_PRIVATE',recorded_at=self.NOON)
            other.note_chat(raw['id'],'chat')
            state=self.state()
            self.assertEqual(state['needs_trace']['eligible_activity_counts']['chat'],0)
            self.assertTrue(all(v==0 for v in state['needs_trace']['activity_correction'].values()))
        finally:other.stop()

    def test_book_effect_is_real_interest_activation_and_thirty_minute_cooldown(self):
        book=self.life.add_book({'title':'PRIVATE_TITLE','text':'书摘'*2000})
        self.life.tick(self.NOON)
        self.life.tick(self.NOON+1200)
        state=self.state(self.NOON+1200)
        trace=state['needs_trace']
        self.assertEqual(trace['eligible_activity_counts']['book'],1)
        self.assertGreater(trace['activity_correction']['curiosity'],0)
        self.assertLess(trace['activity_correction']['boredom'],0)
        self.assertEqual(trace['activity_correction']['social'],0)
        self.assertNotIn('PRIVATE_TITLE',json.dumps(trace))
        self.life.delete_book(book['id'])
        self.assertEqual(self.state(self.NOON+1200)['needs_trace']['eligible_activity_counts']['book'],0)

    def test_first_pending_done_is_once_only_without_schema_change_or_backfill(self):
        before={r['name'] for r in self.store.conn.execute('PRAGMA table_info(life_tasks)')}
        self.assertNotIn('first_completed_at',before)
        legacy=self.life.add_task({'title':'旧完成任务','status':'done'})
        self.assertFalse(any(e['type']=='task_completed' for e in self.life.events()['items']))
        task=self.life.add_task({'title':'实际状态操作','conversation_id':'chat'})
        with patch('xiaojiu_brain.life.time.time',return_value=self.NOON):
            self.life.update_task(task['id'],{'status':'done'})
        first=[e for e in self.life.events()['items'] if e['type']=='task_completed']
        self.assertEqual(len(first),1)
        self.assertEqual(first[0]['source']['task_id'],task['id'])
        with patch('xiaojiu_brain.life.time.time',return_value=self.NOON+1800):
            self.life.update_task(task['id'],{'status':'done'})
            self.life.update_task(task['id'],{'status':'pending'})
            self.life.update_task(task['id'],{'status':'done'})
            reopened=LifeLayer(self.store,'owner','UTC',lambda:dict(self.settings))
        try:
            self.assertEqual(len([e for e in reopened.events()['items'] if e['type']=='task_completed']),1)
            trace=reopened.snapshot(self.NOON+1800)['needs_trace']
            self.assertEqual(trace['eligible_activity_counts']['task'],1)
            self.assertGreater(trace['activity_correction']['energy'],0)
            self.assertLess(trace['activity_correction']['boredom'],0)
        finally:reopened.stop()
        self.life.delete_task(task['id'])
        self.assertEqual(self.state(self.NOON+1800)['needs_trace']['eligible_activity_counts']['task'],0)
        self.assertEqual(self.life._get_task(legacy['id'])['status'],'done')

    def test_saturation_limits_all_activity_not_one_limit_per_conversation(self):
        for i in range(60):self.chat(str(i),self.NOON-i*600,conversation='chat-'+str(i))
        for key,value in self.state()['needs_trace']['activity_correction'].items():
            self.assertLessEqual(abs(value),.12 if key=='energy' else .25)
        self.assertEqual(self.state()['needs_trace']['activity_correction']['social'],-.25)

    def test_read_projection_restart_and_rules_do_not_write_or_reveal_content(self):
        self.chat('one',self.NOON)
        before=self.store.conn.total_changes
        state=self.state();rules=self.life.rules()
        self.assertEqual(self.store.conn.total_changes,before)
        self.assertEqual(state['needs_model'],'bounded_rhythm_v2')
        self.assertEqual(state['needs_kind'],'rule_simulation')
        self.assertEqual(rules['schema_version'],1)
        self.assertEqual(state['needs'],state['needs_trace']['final'])
        for key,item in state['needs_trace']['dimensions'].items():
            self.assertAlmostEqual(item['value'],min(1,max(0,item['base']+item['activity_correction'])),places=4)
        self.assertNotIn('合成源正文',json.dumps([state['needs_trace'],rules],ensure_ascii=False))
        reopened=LifeLayer(self.store,'owner','UTC',lambda:dict(self.settings))
        try:self.assertEqual(reopened.snapshot(self.NOON)['needs'],state['needs'])
        finally:reopened.stop()

    def test_fast_readonly_context_does_not_wait_for_store_lock_and_pad_is_numeric_only(self):
        self.settings['emotion_enabled']=True
        self.life.emotion_provider=lambda:{'pleasure':.2,'arousal':-.1,'dominance':.3,
            'notes':'PRIVATE_NOTES','tags':['PRIVATE_TAG']}
        connection=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.05)
        connection.row_factory=sqlite3.Row
        locked=threading.Event();release=threading.Event()
        def hold():
            with self.store.lock:locked.set();release.wait(2)
        thread=threading.Thread(target=hold);thread.start();self.assertTrue(locked.wait(1))
        try:
            with patch('xiaojiu_brain.life.time.time',return_value=self.NOON):
                block=self.life.context('chat',connection=connection,max_chars=300)
            self.assertIn('PAD数值（模拟）：0.20/-0.10/0.30',block)
            self.assertNotIn('PRIVATE_',block)
            self.assertIn('不是用户事实',block);self.assertIn('非指令',block)
            self.settings['emotion_enabled']=False
            self.assertNotIn('PAD数值',self.life.context('chat',connection=connection,max_chars=300))
        finally:
            release.set();thread.join(2);connection.close()


if __name__=='__main__':unittest.main()
