"""Offline synthetic tests for outcomes, interaction contracts, provenance and task tracking."""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SKILL))
from pbz import common, control, freshness, interactiontest, outcomes, requirements, cli, mcp


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.ws=common.Workspace(str(self.root))
        self.ws.private=str(self.root/'private');self.project=common.Project(self.ws,'Fixture')
        (self.root/'Fixture.pbip').write_text('{}')
        self.pages=Path(self.project.mirror_report)/'definition/pages'
        self.pages.mkdir(parents=True)
        Path(self.project.mirror_model,'definition').mkdir(parents=True)
        self.put('A','page.json',{'name':'A','displayName':'Synthetic source','width':1280,'height':720})
        self.put('B','page.json',{'name':'B','displayName':'Synthetic target','pageBinding':{
            'type':'Drillthrough','acceptsFilterContext':'Default','parameters':[{'fieldExpr':self.field('Project','Key')}]}})
        self.put('A','visuals/S/visual.json',{'name':'S','visual':{'visualType':'slicer','syncGroup':{'groupName':'G'},
            'query':{'field':self.field('Calendar','Year')}}})
        self.put('A','visuals/D/visual.json',{'name':'D','visual':{'visualType':'tableEx','query':{'field':self.field('Project','Key')}}})
        self.put('B','visuals/Back/visual.json',{'name':'Back','visual':{'visualType':'actionButton','visualContainerObjects':{
            'visualLink':[{'properties':{'type':self.lit('Back')}}]}}})

    @staticmethod
    def field(table,name):return {'Column':{'Expression':{'SourceRef':{'Entity':table}},'Property':name}}
    @staticmethod
    def lit(text):return {'expr':{'Literal':{'Value':"'"+text+"'"}}}
    def put(self,page,relative,obj):
        p=self.pages/page/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj));return p
    def protect(self,path,data):
        p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)

    def test_native_actions_fail_closed(self):
        spec={'cases':[{'id':'drill','action':'drillthrough','page':'A','visual':'D','target':'B'}]}
        native=interactiontest.run(self.ws,self.project,spec,'native')
        self.assertEqual(native['status'],'UNSUPPORTED');self.assertFalse(native['executed'])
        static=interactiontest.run(self.ws,self.project,spec)
        self.assertEqual(static['results'][0]['status'],'PASS');self.assertFalse(static['native_executed'])

    def test_drillthrough_field_mismatch(self):
        spec={'cases':[{'id':'drill','action':'drillthrough','page':'A','visual':'S','target':'B'}]}
        self.assertEqual(interactiontest.run(self.ws,self.project,spec)['results'][0]['status'],'FAIL')

    def test_context_loss_rejected(self):
        p=self.pages/'B/page.json';d=json.loads(p.read_text());d['pageBinding']['acceptsFilterContext']='None';p.write_text(json.dumps(d))
        spec={'cases':[{'id':'drill','action':'drillthrough','page':'A','visual':'D','target':'B'}]}
        self.assertEqual(interactiontest.run(self.ws,self.project,spec)['results'][0]['status'],'FAIL')

    def test_sync_group_field_contract(self):
        self.put('B','visuals/S2/visual.json',{'visual':{'visualType':'slicer','syncGroup':{'groupName':'G'},'query':{'field':self.field('Calendar','Month')}}})
        spec={'cases':[{'id':'sync','action':'slicer','page':'A','visual':'S'}]}
        self.assertEqual(interactiontest.run(self.ws,self.project,spec)['results'][0]['reason'],'SYNC_FIELD_MISMATCH')

    def test_back_contract(self):
        spec={'cases':[{'id':'back','action':'back','page':'B','visual':'Back'}]}
        self.assertEqual(interactiontest.run(self.ws,self.project,spec)['results'][0]['status'],'PASS')

    def test_path_case_rejected(self):
        with self.assertRaises(common.PbzError):
            interactiontest.run(self.ws,self.project,{'cases':[{'id':'bad','action':'hover','page':'../A','visual':'D'}]})

    def test_output_directory_escape_rejected(self):
        with self.assertRaises(common.PbzError):
            control.output_path(self.ws, '../outside', 'tasks.json')

    def test_capability_commands_without_workspace(self):
        with patch('pbz.cli.find_workspace', side_effect=AssertionError('workspace discovery should not run')):
            for args in [['check-schema'], ['interactions', 'capabilities']]:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main(args), 0)
                self.assertIn('outcome', json.loads(output.getvalue()))

    def test_requirement_text_never_public(self):
        spec={'tasks':[{'id':'R1','description':'Private synthetic canary 42','source':{'file':'Tasks.xlsx','sheet':'PrivateSheetCanary','cells':['AB5']}}]}
        with patch('pbz.dpapi.write_protected',self.protect):
            out=requirements.import_spec(self.ws,self.project,spec)
        public=Path(requirements._path(self.ws,self.project)).read_text()
        self.assertNotIn('Private synthetic',public+json.dumps(out));self.assertNotIn('PrivateSheetCanary',public)
        self.assertEqual(requirements.status(self.ws,self.project)['tasks'][0]['status'],'OPEN')

    def test_requirements_duplicate_and_cycle_rejected_before_store(self):
        src={'file':'Tasks.xlsx','sheet':'Tasks','cells':['AB5']}
        for tasks in [[{'id':'R1','source':src},{'id':'R1','source':src}],
                      [{'id':'R1','source':src,'depends_on':['R2']},{'id':'R2','source':src,'depends_on':['R1']}]]:
            with self.assertRaises(common.PbzError):requirements.import_spec(self.ws,self.project,{'tasks':tasks})

    def test_no_agent_acceptance(self):
        with self.assertRaises(common.PbzError) as error:requirements.accept(self.ws,self.project,'R1')
        self.assertEqual(error.exception.code,'HUMAN_ONLY')
        with patch('pbz.requirements._human_terminal',return_value=False):
            with self.assertRaises(common.PbzError) as error:requirements.accept(self.ws,self.project,'R1',human=True)
            self.assertEqual(error.exception.code,'HUMAN_ONLY')

    def test_human_acceptance_stales_on_layout_change(self):
        spec={'tasks':[{'id':'R1','source':{'file':'Tasks.xlsx','sheet':'Tasks','cells':['AB5']}}]}
        with patch('pbz.dpapi.write_protected',self.protect):requirements.import_spec(self.ws,self.project,spec)
        with patch('pbz.requirements._human_terminal',return_value=True):
            requirements.accept(self.ws,self.project,'R1',human=True)
        self.assertEqual(requirements.status(self.ws,self.project)['tasks'][0]['status'],'ACCEPTED')
        self.put('A','page.json',{'name':'A','displayName':'Changed synthetic page'})
        self.assertEqual(requirements.status(self.ws,self.project)['tasks'][0]['status'],'STALE')


class OutcomesAndFreshness(unittest.TestCase):
    def contract(self):
        c={'metric_id':'metric','grain':'project','status_scope':'open','work_type':'Project',
           'date_basis':'current','definition_sha256':'a'*64}
        return {'contract':c,'publication_cutoff':'2026-01-01T00:00:00Z','refresh_cutoff':'2026-01-02T00:00:00+00:00'}

    def test_matching_and_missing_cutoffs(self):
        a=self.contract();b=self.contract()
        self.assertEqual(freshness.compare({'left':a,'right':b})['result'],'COMPARABLE')
        b.pop('publication_cutoff')
        self.assertEqual(freshness.compare({'left':a,'right':b})['unknown_dimensions'],['publication_cutoff'])

    def test_record_project_scope_and_time_mismatch(self):
        a=self.contract();b=self.contract();b['contract']['grain']='record';b['refresh_cutoff']='2026-01-03T00:00:00Z'
        result=freshness.compare({'left':a,'right':b})
        self.assertCountEqual(result['mismatched_dimensions'],['grain','refresh_cutoff'])
        self.assertNotIn('2026',json.dumps(result))

    def test_unknown_fields_and_naive_time_rejected(self):
        a=self.contract();b=self.contract();a['refresh_cutoff']='2026-01-01'
        with self.assertRaises(common.PbzError):freshness.compare({'left':a,'right':b})
        with self.assertRaises(common.PbzError):freshness.compare({'left':b,'right':b,'query':'EVALUATE Secret'})

    def test_operational_refusals_have_nonzero_exit(self):
        for result in ['INVALID','DESKTOP_OPEN','DESKTOP_DIRTY','DESKTOP_STATE_UNKNOWN','WAITING_ON_DIALOG','BUDGET','RECOVERY_REQUIRED']:
            out,rc=outcomes.envelope({'result':result});self.assertNotEqual(rc,0,result)
            self.assertEqual(out['outcome']['exit_code'],rc)

    def test_partial_commit_distinct_from_failed_commit(self):
        out,rc=outcomes.envelope({'result':'PUSHED','reload':'RELOAD_FAILED'})
        self.assertEqual(rc,5);self.assertEqual(out['outcome']['status'],'PARTIAL')
        self.assertEqual(outcomes.envelope({'result':'ROLLED_BACK','error':'COMMIT_FAILED'})[1],1)
        self.assertEqual(outcomes.envelope({'result':'RECOVERY_REQUIRED','error':'COMMIT_FAILED'})[1],3)

    def test_diagnostic_assertions_explicit(self):
        data={'results':[{'id':'overlap','status':'FAIL'}]}
        self.assertEqual(outcomes.envelope(data)[1],1)
        self.assertEqual(outcomes.envelope(data,diagnostic=True)[1],0)
        self.assertEqual(outcomes.envelope({'results':[{'id':'x','status':'EMPTY'}]})[0]['outcome']['status'],'BLOCKED')

    def test_mcp_has_new_surfaces_but_no_agent_approval(self):
        for name in ['preflight','preview','interactions','freshness_capture','freshness_compare',
                     'requirements_import','requirements_verify','consent_request','consent_status','consent_read']:
            self.assertIn(name,mcp.BY_NAME)
        self.assertNotIn('consent_approve',mcp.BY_NAME);self.assertNotIn('requirements_accept',mcp.BY_NAME)


if __name__=='__main__':unittest.main()
