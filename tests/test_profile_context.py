"""Explicit role/rule configuration and bounded request-only projection."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from xiaojiu_brain.extensions import Extensions
from xiaojiu_brain.policy import estimate_tokens
from xiaojiu_brain.profile_context import build_context


class ProfileContextTests(unittest.TestCase):
    NOW=1_800_000_000.

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'preferences.json'
        with patch('xiaojiu_brain.extensions.time.time',return_value=self.NOW):self.ext=Extensions(self.path)

    def tearDown(self):self.tmp.cleanup()

    def update(self,data,stamp=None):
        with patch('xiaojiu_brain.extensions.time.time',return_value=self.NOW if stamp is None else stamp):
            return self.ext.update_standards(data)

    def add(self,ident,text='Synthetic owner-configured rule.',**fields):
        return self.update({'add':{'id':ident,'text':text,**fields}})

    def test_defaults_and_legacy_upgrade_do_not_invent_or_enable_standards(self):
        self.assertEqual(self.ext.standards(),{'enabled':False,'version':0,'items':[],'history':[]})
        old=copy.deepcopy(self.ext.data);old.pop('standards');old['self']['style_preferences']='Keep legacy style.'
        self.path.write_text(json.dumps(old),encoding='utf8');upgraded=Extensions(self.path)
        self.assertEqual(upgraded.standards(),{'enabled':False,'version':0,'items':[],'history':[]})
        self.assertEqual(upgraded.data['self'],old['self']);self.assertEqual(upgraded.data['emotion'],old['emotion'])
        self.assertEqual(build_context(upgraded,'synthetic-A')['block'],'')

    def test_versions_revisions_history_and_noops_are_explicit_and_atomic(self):
        before=copy.deepcopy(self.ext.data)
        initial=self.update({'enabled':True,'add':{'id':'rule-a','text':'Initial explicit rule.'}})
        self.assertEqual(initial['version'],1);self.assertEqual(initial['items'][0]['revision'],1)
        self.assertEqual(len(initial['history']),1);self.assertEqual(len(initial['history'][0]['changes']),2)
        self.assertEqual(initial['items'][0]['created_at'],self.NOW)
        changed=self.update({'update':{'id':'rule-a','text':'Revised explicit rule.','priority':90}},self.NOW+10)
        item=changed['items'][0]
        self.assertEqual(changed['version'],2);self.assertEqual(item['revision'],2)
        self.assertEqual(item['created_at'],self.NOW);self.assertEqual(item['updated_at'],self.NOW+10)
        self.assertEqual(changed['history'][-1]['changes'][0]['before']['text'],'Initial explicit rule.')
        disk=self.path.read_bytes()
        self.update({'enabled':True,'update':{'id':'rule-a','text':item['text'],'priority':90}})
        self.add('rule-a',item['text'],priority=90)
        self.assertEqual(self.path.read_bytes(),disk);self.assertEqual(self.ext.standards()['version'],2)
        for key in ('self','emotion','emotion_events','settings'):self.assertEqual(self.ext.data[key],before[key])

    def test_invalid_fields_and_combined_patch_never_partially_change_configuration(self):
        invalid_adds=[{'text':''},{'text':'x'*1001},{'text':'bad\x00text'},
          {'text':'x','priority':True},{'text':'x','priority':1.5},{'text':'x','priority':101},
          {'text':'x','enabled':1},{'text':'x','scope':'group'},
          {'text':'x','scope':'owner','conversation_id':'synthetic-A'},
          {'text':'x','scope':'conversation'},{'text':'x','scope':'conversation','conversation_id':''},
          {'text':'x','revision':500},{'text':'x','id':'bad\nkey'}]
        for item in invalid_adds:
            with self.subTest(item=item):
                before=self.path.read_bytes();state=self.ext.standards()
                with self.assertRaises(ValueError):self.update({'enabled':True,'add':item})
                self.assertEqual(self.path.read_bytes(),before);self.assertEqual(self.ext.standards(),state)
        self.add('known')
        for payload in [{'unknown':True},{'enabled':1},{'add':None},{'update':{'id':'known','created_at':0}},
                        {'update':{'id':'known','priority':float('nan')}}]:
            before=self.path.read_bytes()
            with self.assertRaises(ValueError):self.update(payload)
            self.assertEqual(self.path.read_bytes(),before)
        with self.assertRaises(KeyError):self.update({'enabled':True,'update':{'id':'missing','text':'x'}})
        with self.assertRaises(ValueError):self.add('known','Conflicting retry must not overwrite.')

    def test_scope_changes_require_conversation_and_do_not_reuse_a_previous_conversation(self):
        self.add('scoped',scope='conversation',conversation_id='synthetic-A')
        owner=self.update({'update':{'id':'scoped','scope':'owner'}})['items'][0]
        self.assertIsNone(owner['conversation_id'])
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.update({'update':{'id':'scoped','scope':'conversation'}})
        self.assertEqual(self.path.read_bytes(),before)
        changed=self.update({'update':{'id':'scoped','scope':'conversation','conversation_id':'synthetic-B'}})
        self.assertEqual(changed['items'][0]['conversation_id'],'synthetic-B')

    def test_history_bound_item_limit_restart_and_idempotent_delete(self):
        self.add('editing')
        for index in range(55):self.update({'update':{'id':'editing','text':'Revision '+str(index)}})
        section=self.ext.standards();self.assertEqual(section['version'],56);self.assertEqual(len(section['history']),50)
        self.assertEqual(section['history'][-1]['version'],56);self.assertEqual(section['history'][0]['version'],7)
        self.assertEqual(Extensions(self.path).standards(),section)
        for index in range(49):self.add('extra-'+str(index))
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.add('overflow')
        self.assertEqual(self.path.read_bytes(),before)
        deleted=self.ext.delete_standard('editing');self.assertEqual(len(deleted['items']),49)
        self.assertEqual(deleted['history'][-1]['operation'],'delete');disk=self.path.read_bytes()
        self.assertEqual(self.ext.delete_standard('editing'),deleted);self.assertEqual(self.path.read_bytes(),disk)

    def test_failed_save_does_not_commit_version_or_rule_in_memory(self):
        before=copy.deepcopy(self.ext.data);disk=self.path.read_bytes()
        with patch('xiaojiu_brain.extensions.os.replace',side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):self.update({'enabled':True,'add':{'id':'not-committed','text':'x'}})
        self.assertEqual(self.ext.data,before);self.assertEqual(self.path.read_bytes(),disk)

    def test_context_off_ignores_memories_and_host_snapshot_and_never_auto_promotes_them(self):
        self.ext.data['memories']=[{'text':'UNTRUSTED_MEMORY_MUST_NOT_BECOME_RULE','priority':100}]
        self.ext.data['host_persona']={'text':'HOST_SNAPSHOT_MUST_NOT_BE_INJECTED'}
        self.ext.update_self({'text':'SELF_OFF_MARKER'})
        self.add('disabled-section','RULE_OFF_MARKER')
        result=build_context(self.ext,'synthetic-A')
        self.assertEqual(result['block'],'');self.assertEqual(result['trace']['included_ids'],[])
        self.assertEqual(result['trace']['reason'],'disabled')
        self.update({'enabled':True})
        result=build_context(self.ext,'synthetic-A')
        self.assertIn('RULE_OFF_MARKER',result['block']);self.assertNotIn('SELF_OFF_MARKER',result['block'])
        self.assertNotIn('UNTRUSTED_MEMORY',result['block']);self.assertNotIn('HOST_SNAPSHOT',result['block'])

    def test_context_priority_is_stable_scope_aware_and_below_higher_level_instructions(self):
        self.add('z-high','HIGH_Z',priority=90)
        self.add('a-high','HIGH_A',priority=90)
        self.add('low','LOW',priority=10)
        self.add('other','OTHER_CONVERSATION_SECRET',scope='conversation',conversation_id='synthetic-B',priority=100)
        self.add('disabled','DISABLED_ITEM',enabled=False,priority=100)
        self.update({'enabled':True})
        result=build_context(self.ext,'synthetic-A')
        self.assertEqual(result['trace']['standard_ids'],['a-high','z-high','low'])
        self.assertLess(result['block'].index('HIGH_A'),result['block'].index('HIGH_Z'))
        self.assertNotIn('OTHER_CONVERSATION_SECRET',result['block']);self.assertNotIn('DISABLED_ITEM',result['block'])
        self.assertNotIn('standard:other',result['trace']['omitted_ids'])
        self.assertIn('服从系统、开发者等更高层指令',result['block'])
        self.assertEqual(result['trace']['priority_scope'],'user_layer_only')
        other=build_context(self.ext,'synthetic-B')
        self.assertEqual(other['trace']['standard_ids'][0],'other')

    def test_self_style_and_notes_have_stable_order_and_independent_enable_switch(self):
        self.ext.update_self({'style_preferences':'Synthetic concise style.','notes':[
          {'id':'new','text':'Newer note.','created_at':20},{'id':'old','text':'Older note.','created_at':10}]})
        self.ext.update_settings({'self_notes_enabled':True})
        result=build_context(self.ext,'synthetic-A')
        self.assertTrue(result['trace']['style_included']);self.assertEqual(result['trace']['self_note_ids'],['old','new'])
        self.assertLess(result['block'].index('Synthetic concise style.'),result['block'].index('Older note.'))
        self.assertLess(result['block'].index('Older note.'),result['block'].index('Newer note.'))
        self.assertEqual(result['trace']['standard_ids'],[])

    def test_budget_preserves_complete_items_reports_omissions_and_prioritizes_standards(self):
        long_text='LONG_RULE_'+'x'*990
        self.add('too-large',long_text,priority=100)
        self.add('fits','Complete short standard.',priority=50)
        self.update({'enabled':True})
        self.ext.update_self({'style_preferences':'STYLE_TOO_LARGE_'+'y'*2000,'text':'Complete short self note.'})
        self.ext.update_settings({'self_notes_enabled':True})
        result=build_context(self.ext,'synthetic-A')
        self.assertEqual(result['trace']['standard_ids'],['fits'])
        self.assertIn('standard:too-large',result['trace']['omitted_ids'])
        self.assertIn('self:style_preferences',result['trace']['omitted_ids'])
        self.assertIn('Complete short standard.',result['block']);self.assertNotIn('LONG_RULE_',result['block'])
        self.assertNotIn('STYLE_TOO_LARGE_',result['block']);self.assertIn('Complete short self note.',result['block'])
        self.assertLessEqual(estimate_tokens(result['block']),384)
        self.assertEqual(result['trace']['estimated_tokens'],estimate_tokens(result['block']))
        self.assertEqual(result['trace']['omitted_count'],len(result['trace']['omitted_ids']))
        self.assertIn('profile_context_omitted_budget',result['degraded'])
        none=build_context(self.ext,'synthetic-A',token_limit=1)
        self.assertEqual(none['block'],'');self.assertEqual(none['trace']['estimated_tokens'],0)
        self.assertEqual(none['trace']['omitted_count'],none['trace']['eligible_count'])

    def test_projection_is_read_only_versioned_and_does_not_depend_on_store_or_retrieval(self):
        self.add('current','CURRENT_RULE');self.update({'enabled':True})
        self.ext.data['store']=None  # An unrelated object is not consulted.
        before=self.path.read_bytes();first=build_context(self.ext,'synthetic-A');second=build_context(self.ext,'synthetic-A')
        self.assertEqual(first,second);self.assertEqual(self.path.read_bytes(),before)
        self.update({'update':{'id':'current','text':'CHANGED_RULE'}})
        after=build_context(self.ext,'synthetic-A')
        self.assertNotEqual(first['trace']['profile_version'],after['trace']['profile_version'])
        self.assertGreater(after['trace']['standards_version'],first['trace']['standards_version'])
        self.assertNotIn('CURRENT_RULE',after['block'])

    def test_context_input_bounds_and_clock_rollback_keep_revision_time_consistent(self):
        self.add('time','Initial time rule.')
        self.update({'update':{'id':'time','text':'Updated time rule.'}},self.NOW-100)
        item=self.ext.standards()['items'][0]
        self.assertGreaterEqual(item['updated_at'],item['created_at'])
        for conversation in ['',None,'x'*513,'bad\x00conversation']:
            with self.assertRaises(ValueError):build_context(self.ext,conversation)
        for budget in [True,0,385,384.0,float('inf')]:
            with self.assertRaises(ValueError):build_context(self.ext,'synthetic-A',budget)


if __name__=='__main__':unittest.main()
