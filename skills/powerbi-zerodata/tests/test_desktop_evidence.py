"""Synthetic unit tests for Desktop lifecycle, scoped evidence and review packets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SKILL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SKILL))
from pbz import desktop, evidence, handoff
from pbz.common import PbzError


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.project=SimpleNamespace(name='Synthetic',pbip='Synthetic.pbip',ws=SimpleNamespace(private='synthetic-private'))
        self.inst={'pid':123,'via':'bridge','unsaved':False}

    def test_dirty_is_tristate(self):
        self.assertIsNone(desktop._dirty_state({}))
        self.assertIsNone(desktop._dirty_state({'hasUnsavedChanges':None}))
        self.assertIs(desktop._dirty_state({'hasUnsavedChanges':False}),False)
        self.assertIs(desktop._dirty_state({'hasUnsavedChanges':False,'isDirty':True}),True)

    def test_process_fallback_requires_exact_path_token(self):
        self.project.pbip='C:\\Synthetic.pbip'
        with patch.object(desktop,'bridge_status',return_value=[]),patch.object(desktop,'processes',return_value=[{'pid':1,'cmd':'"Desktop.exe" "C:\\Synthetic.pbip.other"'}]):
            self.assertIsNone(desktop.find_instance(self.project))
        with patch.object(desktop,'bridge_status',return_value=[]),patch.object(desktop,'processes',return_value=[{'pid':1,'cmd':'"Desktop.exe" "C:\\Synthetic.pbip"'}]):
            self.assertEqual(desktop.find_instance(self.project)['pid'],1)

    def test_multiple_project_instances_are_refused(self):
        bridge=[{'pid':1,'paths':['Synthetic.pbip'],'unsaved':False}, {'pid':2,'paths':['Synthetic.pbip'],'unsaved':True}]
        with patch.object(desktop,'bridge_status',return_value=bridge),patch.object(desktop,'processes',return_value=[]):
            with self.assertRaises(PbzError) as ctx:desktop.find_instance(self.project)
            self.assertEqual(ctx.exception.code,'AMBIGUOUS_DESKTOP')

    def test_bridge_and_process_same_pid_are_one_instance(self):
        bridge=[{'pid':1,'paths':['Synthetic.pbip'],'unsaved':False}]
        with patch.object(desktop,'bridge_status',return_value=bridge),patch.object(desktop,'processes',return_value=[{'pid':1,'cmd':'Desktop.exe Synthetic.pbip'}]):
            self.assertIs(desktop.find_instance(self.project)['unsaved'],False)

    def test_status_requires_handshake(self):
        with patch.object(desktop,'find_instance',return_value=self.inst),patch.object(desktop,'engine_port',return_value=99),patch.object(desktop,'engine_call',return_value={'ok':False}):
            self.assertEqual(desktop.status(self.project)['engine'],'NOT_READY')

    def test_existing_open_waits_for_handshake(self):
        with patch.object(desktop,'find_instance',return_value=self.inst),patch.object(desktop,'engine_port',return_value=99),patch.object(desktop,'engine_call',side_effect=[{'ok':False,'class':'ENGINE_UNAVAILABLE'},{'ok':True}]) as connect,patch.object(desktop.time,'sleep'),patch.object(desktop.time,'monotonic',return_value=0):
            result=desktop.open_(self.project,wait=10)
        self.assertEqual(connect.call_count,2)
        self.assertEqual(result['result'],'READY')
        self.assertEqual(result['action'],'already_open')

    def test_existing_not_ready_times_out(self):
        with patch.object(desktop,'find_instance',return_value=self.inst),patch.object(desktop,'engine_port',return_value=None):
            result=desktop.open_(self.project,wait=0)
        self.assertEqual(result['result'],'TIMEOUT')
        self.assertEqual(result['engine'],'NOT_READY')

    def test_new_open_never_accepts_engine_unavailable(self):
        with patch.object(desktop,'find_instance',side_effect=[None,self.inst]),patch.object(desktop.os,'startfile',create=True) as launch,patch.object(desktop,'engine_port',return_value=99),patch.object(desktop,'engine_call',return_value={'ok':False,'class':'ENGINE_UNAVAILABLE'}):
            result=desktop.open_(self.project,wait=0)
        launch.assert_called_once_with('Synthetic.pbip')
        self.assertEqual(result['result'],'TIMEOUT')

    def test_close_has_canonical_blocked_result(self):
        with patch.object(desktop,'find_instance',return_value=self.inst),patch.object(desktop,'_ps',side_effect=['','alive']):
            result=desktop.close(self.project,wait=0)
        self.assertEqual(result['action'],'WAITING_ON_DIALOG')
        self.assertEqual(result['result'],'WAITING_ON_DIALOG')
        self.assertEqual(result['reason'],'UNKNOWN')

    def test_close_not_open_is_idempotent(self):
        with patch.object(desktop,'find_instance',return_value=None):
            self.assertEqual(desktop.close(self.project)['result'],'NOT_OPEN')

    def test_reload_blocks_unknown_and_dirty(self):
        for state,expected in [(None,'DESKTOP_STATE_UNKNOWN'),(True,'DESKTOP_DIRTY')]:
            with self.subTest(state=state),patch.object(desktop,'find_instance',return_value={**self.inst,'unsaved':state}),patch.object(desktop.os.path,'exists',return_value=True),patch.object(desktop.subprocess,'run') as run:
                self.assertEqual(desktop.reload(self.project),expected)
                run.assert_not_called()

    def test_reload_preserves_string_contract(self):
        with patch.object(desktop,'find_instance',return_value=self.inst),patch.object(desktop.os.path,'exists',return_value=True),patch.object(desktop.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'{"ok":true}')):
            self.assertEqual(desktop.reload(self.project),'RELOADED')

    def test_require_port_rejects_subset_and_extra_tables(self):
        for live in [['One'],['One','Two','Other'],[]]:
            with self.subTest(live=live),patch.object(desktop,'find_instance',return_value=self.inst),patch.object(desktop,'engine_port',return_value=99),patch.object(desktop,'engine_call',return_value={'ok':True,'tables':live}),patch('pbz.tmdl.load_model',return_value=SimpleNamespace(tables={'One':None,'Two':None})):
                self.project.model_dir='synthetic-model'
                with self.assertRaises(PbzError) as ctx:desktop.require_port(self.project)
                self.assertEqual(ctx.exception.code,'ENGINE_MISMATCH')

    def test_require_port_allows_documented_auto_date_tables(self):
        with patch.object(desktop,'find_instance',return_value=self.inst),patch.object(desktop,'engine_port',return_value=99),patch.object(desktop,'engine_call',return_value={'ok':True,'tables':['One','LocalDateTable_fixture','DateTableTemplate_fixture']}),patch('pbz.tmdl.load_model',return_value=SimpleNamespace(tables={'One':None})):
            self.project.model_dir='synthetic-model'
            self.assertEqual(desktop.require_port(self.project),99)


class EvidenceTests(unittest.TestCase):
    def test_reload_legacy_and_typed_payloads(self):
        for value in ['RELOADED',{'ok':True}]:
            stage,summary=evidence.summarize('desktop','reload',{'reload':value})
            self.assertEqual(stage,'LOADED');self.assertTrue(summary['ready'])
        self.assertFalse(evidence.summarize('desktop','reload',{'reload':'RELOAD_FAILED'})[1]['ready'])

    def test_budget_never_implies_execution(self):
        for verb,action in [('smoke',None),('snapshot','compare'),('check',None)]:
            summary=evidence.summarize(verb,action,{'status':'BUDGET'})[1]
            self.assertEqual(summary['outcome'],'BLOCKED');self.assertFalse(summary['executed'])

    def test_unknown_verdict_text_is_not_persisted(self):
        secret='Invented confidential diagnostic'
        events=evidence._scoped_summaries('check',None,{'results':[{'id':'safe-id','status':secret}]})
        self.assertNotIn(secret,json.dumps(events))
        self.assertEqual(events[0][2]['status'],'UNKNOWN')
        self.assertEqual(events[0][2]['outcome'],'INCOMPLETE')

    def test_existing_handoff_link_is_not_new_evidence(self):
        self.assertIsNone(evidence.summarize('handoff',None,{'result':'NOTHING_NEW','file':'pbz-workspace/HANDOFF.md'}))

    def test_health_includes_processing_state(self):
        summary=evidence.summarize('health',None,{'ok':True,'errors':[],'unprocessed_tables':['Synthetic']})[1]
        self.assertEqual(summary['unprocessed_tables'],1);self.assertEqual(summary['outcome'],'INCOMPLETE')

    def test_push_reload_records_both_stages(self):
        events=evidence._scoped_summaries('push',None,{'result':'PUSHED','findings':[],'reload':'RELOADED'})
        self.assertEqual([e[0] for e in events],['BOUND','LOADED'])
        self.assertEqual(events[1][2]['outcome'],'PASS')

    def test_binding_coverage_requires_explicit_receipts(self):
        summary=evidence.summarize('push',None,{'result':'PUSHED','findings':[]})[1]
        self.assertEqual(summary['outcome'],'INCOMPLETE')
        self.assertIsNone(summary['tom'])
        passed=[{'code':'TOM_PASSED','severity':'info'},{'code':'REPORT_SCHEMA_PASSED','severity':'info'}]
        summary=evidence.summarize('push',None,{'result':'PUSHED','findings':passed})[1]
        self.assertEqual(summary['outcome'],'PASS')
        failed=passed+[{'code':'TMDL_SYNTAX','severity':'error'}]
        self.assertEqual(evidence.summarize('push',None,{'result':'INVALID','findings':failed})[1]['validation']['tom'],'FAIL')

    def test_binding_and_synthetic_evidence_stale_after_mirror_edit(self):
        item={'mirror_rev':'old','real_rev':'r1'}
        for stage in ['BOUND','SYNTHETIC','EXECUTED']:
            self.assertEqual(evidence._status(item,'new','r1',False,stage),'STALE')

    def test_partial_smoke_does_not_claim_complete_filtering(self):
        events=evidence._scoped_summaries('smoke',None,{'visuals':[{'page':'A','status':'OK','note':'filters partial'}]})
        self.assertEqual(events[0][2]['coverage'],'QUERY_ONLY')
        self.assertEqual(events[0][2]['outcome'],'INCOMPLETE')

    def test_review_retains_independent_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            private=os.path.join(tmp,'private')
            os.makedirs(private,exist_ok=True)
            ws=SimpleNamespace(root=tmp,private=private,pbz_dir=os.path.join(tmp,'.pbz'),mirror=os.path.join(tmp,'pbz-workspace'))
            project=SimpleNamespace(name='Synthetic')
            mrev,rrev='m0123456789ab','r0123456789ab'
            with patch.object(evidence,'mirror_rev',return_value=mrev),patch.object(evidence,'real_rev',return_value=rrev),patch('pbz.changes.run',return_value={'result':'SAME'}),patch('pbz.consumers.load_registry',return_value={'consumers':[]}),patch.object(evidence,'_suggested_tests',return_value=[]):
                evidence.record(ws,project,'health',None,{'ok':True,'errors':[]})
                evidence.record(ws,project,'check',None,{'results':[{'id':'one','status':'FAIL'},{'id':'two','status':'PASS'}]})
                evidence.record(ws,project,'check',None,{'results':[{'id':'one','status':'PASS'}]})
                evidence.record(ws,project,'smoke',None,{'visuals':[{'page':'A','status':'OK'},{'page':'B','status':'OK'}]})
                out=evidence.review(ws,project)
                row=next(e for e in out['evidence'] if e['stage']=='EXECUTED')
                self.assertEqual(len(row['items']),5)
                self.assertEqual(row['status'],'CURRENT')
                self.assertEqual(next(i for i in row['items'] if i['scope']=='check:one')['summary']['status'],'PASS')
                self.assertTrue(Path(ws.mirror,'REVIEW.md').exists())


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ws=SimpleNamespace(pbz_dir=os.path.join(self.tmp.name,'.pbz'),mirror=os.path.join(self.tmp.name,'pbz-workspace'))
        self.project=SimpleNamespace(name='Synthetic',mirror_model=os.path.join(self.tmp.name,'model'),mirror_report=os.path.join(self.tmp.name,'report'))
        Path(self.ws.pbz_dir).mkdir()

    def push(self,stamp,project='Synthetic'):
        with open(Path(self.ws.pbz_dir,'push-log.jsonl'),'a',encoding='utf-8') as f:f.write(json.dumps({'project':project,'stamp':stamp,'objects':['Synthetic.Report/definition/pages/page/page.json']})+'\n')

    def build(self):
        with patch.object(handoff.tmdl,'load_model',return_value=None),patch.object(handoff,'field_usage',return_value={}),patch.object(evidence,'mirror_rev',return_value='m1'),patch.object(evidence,'real_rev',return_value='r1'):
            return handoff.build(self.ws,self.project)

    def test_regeneration_preserves_notes_and_pending_cursor(self):
        self.push('001');first=self.build();alias=Path(self.ws.mirror,'HANDOFF.md')
        packet=Path(self.ws.mirror,first['packet'].split('/',1)[1]);original=packet.read_bytes()
        with alias.open('a',encoding='utf-8') as f:f.write('\nReviewer synthetic note\n')
        second=self.build()
        self.assertEqual(first['packet'],second['packet'])
        self.assertIn('Reviewer synthetic note',alias.read_text(encoding='utf-8'))
        self.assertEqual(packet.read_bytes(),original)
        state=json.loads(Path(self.ws.pbz_dir,'handoff.json').read_text())['projects']['Synthetic']
        self.assertEqual(state['generated'],'001');self.assertNotIn('accepted',state)
        self.push('002');third=self.build();self.assertNotEqual(third['packet'],first['packet'])
        self.assertEqual(packet.read_bytes(),original)
        self.assertEqual(alias.read_text(encoding='utf-8').count('Reviewer synthetic note'),1)

    def test_acceptance_requires_matching_revision(self):
        self.push('001');self.build()
        self.assertFalse(handoff.accept_generated(self.ws,self.project,'different','r1'))
        self.assertTrue(handoff.accept_generated(self.ws,self.project,'m1','r1'))
        existing=self.build()
        self.assertEqual(existing['result'],'NOTHING_NEW')
        self.assertEqual(existing['file'],'pbz-workspace/HANDOFF.md')

    def test_tampered_packet_cannot_be_accepted_or_overwritten(self):
        self.push('001');out=self.build()
        packet=Path(self.ws.mirror,out['packet'].split('/',1)[1]);packet.write_text('Changed synthetic packet',encoding='utf-8')
        self.assertFalse(handoff.accept_generated(self.ws,self.project,'m1','r1'))
        with self.assertRaises(PbzError) as ctx:self.build()
        self.assertEqual(ctx.exception.code,'PACKET_CONFLICT')

    def test_legacy_generation_cursor_does_not_consume_review(self):
        Path(self.ws.pbz_dir,'handoff.json').write_text(json.dumps({'last':'999'}),encoding='utf-8')
        Path(self.ws.mirror).mkdir()
        Path(self.ws.mirror,'HANDOFF.md').write_text('Legacy checklist\nManual synthetic note\n',encoding='utf-8')
        self.push('001');self.build()
        self.assertIn('Manual synthetic note',Path(self.ws.mirror,'HANDOFF.md').read_text(encoding='utf-8'))

    def test_packet_writer_preserves_external_review_notes(self):
        first=handoff.write_packet(self.ws,'REVIEW.md','Generated one\n','reviews')
        alias=Path(self.ws.mirror,'REVIEW.md')
        with alias.open('a',encoding='utf-8') as f:f.write('Manual review note\n')
        second=handoff.write_packet(self.ws,'REVIEW.md','Generated two\n','reviews')
        self.assertNotEqual(first,second)
        self.assertIn('Manual review note',alias.read_text(encoding='utf-8'))
        self.assertNotIn('Generated one',alias.read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
