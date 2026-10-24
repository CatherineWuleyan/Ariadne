import copy, importlib.util, json, pathlib, sys, unittest
REPO=pathlib.Path('/Users/catherinewu/Documents/gaia-ir');sys.path.insert(0,str(REPO/'ariadne'))
local=pathlib.Path(__file__).with_name('evidence_update.py');path=local if local.exists() else pathlib.Path(__file__).parents[1]/'evidence_update.py'
spec=importlib.util.spec_from_file_location('ariadne.evidence_update',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def node(i,summary=None,entity=None,role=None):return {'id':i,'entity_id':entity or i,'summary':summary or i,'details':{'theory_kind':role} if role else {}}
def edge(s,t,kind='supports',directed=True):return {'id':s+'>'+t+kind,'source':s,'target':t,'semantic_type':kind,'details':{'directed':directed}}
def graph():return {'nodes':[node('a'),node('b'),node('bridge',role='bridge'),node('answer',role='root'),node('other')],'edges':[edge('a','bridge'),edge('bridge','answer')],'answer_layer':{'roots':[{'id':'answer'}],'root_ids':['answer'],'complete_routes':{'answer':[{'operator':'conjunction','premises':['bridge','b']}]}}}
class UpdateTests(unittest.TestCase):
 def test_remove_propagates_and_withdraws(self):
  g=graph();snapshot=copy.deepcopy(g);u=m.prepare_update(g,[{'op':'remove','id':'a'}]);self.assertEqual(g,snapshot);self.assertEqual(u['affected_roots'],['answer']);self.assertEqual(set(u['invalidated_ids']),{'bridge','answer'});self.assertFalse({'a','bridge','answer'} & {n['id'] for n in u['working_view']['nodes']})
 def test_conjunction_required_premise_triggers(self):
  u=m.prepare_update(graph(),[{'op':'remove','id':'b'}]);self.assertEqual(u['affected_roots'],['answer']);self.assertEqual(u['impact_paths']['answer'][0]['relation'],'conjunction_requirement')
 def test_symmetric_conjunction_not_entailment(self):
  g=graph();g['edges']=[edge('a','b','conjunction',False)];g['answer_layer']['complete_routes']={};u=m.prepare_update(g,[{'op':'remove','id':'a'}]);self.assertEqual(u['affected_roots'],[]);self.assertNotIn('b',u['impact_paths'])
 def test_unrelated_no_reassessment(self):
  u=m.prepare_update(graph(),[{'op':'add','node':node('new')}]);self.assertEqual(u['affected_roots'],[])
 def test_duplicate_origin_no_change(self):
  u=m.prepare_update(graph(),[{'op':'duplicate','id':'a','new_id':'a-copy'}]);self.assertEqual(u['changed_ids'],[]);self.assertEqual(u['affected_roots'],[]);self.assertEqual(u['aliases'],{'a-copy':'a'})
 def test_incompatible_duplicate_not_silently_merged(self):
  g=graph();g['nodes'].append(node('a2','incompatible',entity='a'))
  with self.assertRaises(ValueError):m.canonicalize(g)
 def test_replace_scrubs_old_local_fields(self):
  g=graph();g['nodes'][0]['details']['old_cached_value']='SECRET_OLD';u=m.prepare_update(g,[{'op':'replace','id':'a','text':'NEW'}]);n=next(n for n in u['working_view']['nodes'] if n['id']=='a');self.assertNotIn('SECRET_OLD',json.dumps(n));self.assertEqual(n['summary'],'NEW')
 def test_new_link_to_existing_dependency_propagates(self):
  u=m.prepare_update(graph(),[{'op':'add','node':node('new'),'edges':[edge('new','bridge','bounds')]}]);self.assertEqual(u['affected_roots'],['answer'])
 def test_cycles_terminate(self):
  g=graph();g['edges']+=[edge('bridge','a')];self.assertEqual(m.prepare_update(g,[{'op':'replace','id':'a','text':'new'}])['affected_roots'],['answer'])
 def test_missing_dependency_audited(self):
  g=graph();g['nodes'][2]['details']['premises']=['missing'];self.assertTrue(m.prepare_update(g,[])['dependency_issues'])
 def test_actual_physics_impact_from_edges(self):
  g=json.loads((REPO/'outputs/physics/final/graph_with_answer.reviewed.json').read_text());u=m.prepare_update(g,[{'op':'remove','id':'knowledge:papers:paper_db13a75e::claim_4'}]);self.assertEqual(set(u['affected_roots']),{'theory:R1','theory:R2'});self.assertIn('theory:M2',u['invalidated_ids']);self.assertTrue(u['impact_paths']['theory:R2']);self.assertFalse(set(u['invalidated_ids']) & {n['id'] for n in u['working_view']['nodes']})
 def test_bad_judgments_rejected(self):
  r={'question_id':'answer','status':'unknown','answer':None,'premises':[],'provenance':[],'evidence_path':[],'conditions':[{'key':'reason','value':'缺证据'},{'key':'updated_conclusion','value':'尚不能确认'}]};m.validate_judgment(r,[{'id':'a'}],'answer');r['provenance']=['invented']
  with self.assertRaises(ValueError):m.validate_judgment(r,[{'id':'a'}],'answer')
 def test_old_answers_not_retrieval_evidence(self):
  u=m.prepare_update(graph(),[]);self.assertNotIn('answer',{n['id'] for n in u['working_view']['nodes']});self.assertNotIn('answer_layer',u['working_view'])
 def test_native_view_keeps_unchanged_answers_and_semantic_caveat(self):
  ql,_=m.native_imports();g=graph();initial=m.prepare_update(g,[])['working_view'];root={'id':'answer','code':'R1','title_zh':'测试命题','title_en':'test','statement':'test proposition','scope':'test','criteria':'test','answer_type':'method_route','seed_claim_ids':['a']}
  links={'R1':[{'claim_id':'a','relation':'support','reason':'test input'}]};layer=ql.build_answer_layer(initial,{'zh':'test'},[root],links);original=copy.deepcopy(layer);answers={'answer':{'status':'unknown','updated_conclusion':'证据不足，保留条件'}}
  for patch in ([{'op':'add','node':node('irrelevant')}],[{'op':'duplicate','id':'a','new_id':'copy_a'}]):
   state=m.prepare_update(g,patch);restored,view=m.restore_answers(ql,state['working_view'],layer,answers);self.assertIn('answer',{n['id'] for n in view['nodes']});self.assertIn('unknown',restored['roots'][0]['caveat']);self.assertIn('证据不足',next(n for n in view['nodes'] if n['id']=='answer')['details']['caveat']);self.assertEqual(layer,original)
 def test_zero_support_answer_not_lost(self):
  ql,_=m.native_imports();g=graph();working=m.prepare_update(g,[])['working_view'];root={'id':'answer','code':'R1','title_zh':'测试','title_en':'test','statement':'claim','scope':'test','criteria':'test','answer_type':'method_route','seed_claim_ids':['a']};layer=ql.build_answer_layer(working,{'zh':'test'},[root],{'R1':[{'claim_id':'a','relation':'support','reason':'test'}]});other=dict(root,id='answer2',code='R2',statement='unsupported claim');answers={i:{'status':'unknown','updated_conclusion':'证据不足'} for i in ['answer','answer2']};restored,view=m.restore_answers(ql,working,layer,answers,[root,other]);r=next(r for r in restored['roots'] if r['id']=='answer2');self.assertEqual(r['verdict_class'],'exploratory');self.assertEqual(r['support_ids'],[]);self.assertEqual(r['stats']['support_claim_count'],0);self.assertIn('answer2',{n['id'] for n in view['nodes']});self.assertFalse(any(e['target']=='answer2' for e in restored['answer_edges']))
if __name__=='__main__':unittest.main()
