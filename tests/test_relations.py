import unittest
from xiaojiu_brain.relations import relation_candidates

class RelationNavigationTests(unittest.TestCase):
    def test_one_hop_finds_an_existing_named_subject_without_choosing_a_truth(self):
        docs={k:{'owner':'u','conversation_id':'c','kind':'fact','status':'active'} for k in ['a','b','c']}
        claims={'a':{'subject':'小林','predicate':'主管','value':'王工'},
            'b':{'subject':'王工','predicate':'生日','value':'六月'},
            'c':{'subject':'王工','predicate':'生日','value':'七月'}}
        ids,trace=relation_candidates(docs,claims,'小林主管的生日？','u','c')
        self.assertEqual(ids,['a','b','c']);self.assertFalse(trace['truth_certified']);self.assertEqual(trace['hop_limit'],1)
        self.assertNotIn('王工',str(trace))
    def test_scope_and_inactive_rows_cannot_supply_edges(self):
        docs={'a':{'owner':'u','conversation_id':'c','kind':'fact'},
            'b':{'owner':'other','conversation_id':'c','kind':'fact'},
            'c':{'owner':'u','conversation_id':'other','kind':'fact'},
            'd':{'owner':'u','conversation_id':'c','kind':'fact','status':'archived'}}
        claims={k:{'subject':'小林','predicate':'关系','value':'别人'} for k in docs}
        ids,_=relation_candidates(docs,claims,'小林','u','c');self.assertEqual(ids,['a'])
    def test_no_alias_guessing_substring_collision_or_unbounded_traversal(self):
        docs={k:{'owner':'u','conversation_id':'c','kind':'fact'} for k in ['a','b','c']}
        claims={'a':{'subject':'Ann','predicate':'knows','value':'Beth'},
            'b':{'subject':'Beth','predicate':'knows','value':'Cara'},
            'c':{'subject':'Cara','predicate':'color','value':'blue'}}
        self.assertEqual(relation_candidates(docs,claims,'Joanne','u','c')[0],[])
        self.assertEqual(relation_candidates(docs,claims,'Ann birthday','u','c')[0],['a','b'])
        self.assertEqual(relation_candidates(docs,claims,'ＡＮＮ birthday','u','c',limit=1)[0],['a'])
