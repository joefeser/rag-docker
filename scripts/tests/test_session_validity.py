"""Retained-session metadata and explicit historical export policy."""
import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR',str(Path(__file__).resolve().parents[2]/'api')))
from fastapi.testclient import TestClient
from config import settings
from main import app
from models.schemas import SessionResponse
from services import goldstandard as gs


def session():
    pairs=[{'pair_id':f'pair{i}','question':'Inert question','answer':'Inert answer',
            'contexts':['Inert historical context'],'ground_truth':'Inert truth',
            'source_file':'inert.txt','chunk_index':i,'status':status}
           for i,status in enumerate(('approved','edited','pending','rejected'))]
    return {'session_id':'gs_47abcdef','collection':'ValidityFixture','status':'completed',
            'pairs_total':4,'pairs_completed':4,'pairs':pairs}


class ValidityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.upload=patch.object(settings,'upload_dir',self.temp.name);self.upload.start();self.addCleanup(self.upload.stop)
        self.original=gs._sessions;gs._sessions={};self.addCleanup(setattr,gs,'_sessions',self.original)
        self.data=session();gs._sessions[self.data['session_id']]=self.data
        self.client=TestClient(app)  # No context manager/startup sweep.

    def test_legacy_sessions_are_readable_with_clear_default_validity(self):
        response=self.client.get('/goldstandard/session/'+self.data['session_id'])
        self.assertEqual(response.status_code,200,response.text)
        body=response.json()
        self.assertFalse(body['stale']);self.assertFalse(body['orphaned'])
        self.assertIsNone(body['stale_reason']);self.assertIsNone(body['orphaned_at'])
        self.assertEqual(body['pairs'],self.data['pairs'])

    def test_each_recorded_flag_reason_and_timestamp_reaches_http_response(self):
        for flag in ('stale','orphaned'):
            self.data.update({flag:True,flag+'_reason':'Synthetic '+flag+' reason',flag+'_at':'2026-09-28T00:00:00+00:00'})
        body=self.client.get('/goldstandard/session/'+self.data['session_id']).json()
        for flag in ('stale','orphaned'):
            self.assertTrue(body[flag]);self.assertEqual(body[flag+'_reason'],self.data[flag+'_reason'])
            self.assertEqual(body[flag+'_at'],self.data[flag+'_at'])
        self.assertEqual(body['pairs'],self.data['pairs'])

    def test_real_marking_and_restart_retain_metadata_and_pairs(self):
        gs._save_session_sync(self.data)
        self.assertEqual(gs.mark_stale('ValidityFixture','Synthetic rechunk'),1)
        self.assertEqual(gs.mark_orphaned('ValidityFixture','Synthetic deletion'),1)
        expected=copy.deepcopy(gs.get_session(self.data['session_id']))
        gs._sessions={};gs.load_sessions_from_disk()
        self.assertEqual(gs.get_session(self.data['session_id']),expected)
        response=self.client.get('/goldstandard/session/'+self.data['session_id'])
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['stale']);self.assertTrue(response.json()['orphaned'])

    def test_any_historical_flag_blocks_export_before_filesystem_write(self):
        for flags in ({'stale':True},{'orphaned':True},{'stale':True,'orphaned':True}):
            self.data.pop('stale',None);self.data.pop('orphaned',None);self.data.update(flags)
            before=copy.deepcopy(self.data)
            for option in ({},{'allow_historical':False}):
                with patch.object(gs,'_save_export_sync') as write:
                    response=self.client.post('/goldstandard/save',json={'session_id':self.data['session_id'],**option})
                    self.assertEqual(response.status_code,409,response.text)
                    self.assertEqual(response.json()['error']['code'],'HISTORICAL_SESSION')
                    write.assert_not_called()
            self.assertEqual(self.data,before)
            self.assertEqual(list(Path(self.temp.name).iterdir()),[])

    def test_explicit_export_keeps_ragas_format_reports_validity_and_preserves_history(self):
        self.data.update(stale=True,stale_reason='Synthetic rechunk',stale_at='2026-09-28T00:00:00+00:00')
        before=copy.deepcopy(self.data)
        response=self.client.post('/goldstandard/save',json={'session_id':self.data['session_id'],
                    'filename':'historical.json','allow_historical':True})
        self.assertEqual(response.status_code,200,response.text)
        body=response.json();self.assertTrue(body['historical'])
        self.assertTrue(body['session_validity']['stale'])
        self.assertEqual(body['session_validity']['stale_reason'],'Synthetic rechunk')
        self.assertEqual((body['pairs_saved'],body['pairs_excluded']),(2,2))
        rows=json.loads((Path(self.temp.name)/'historical.json').read_text())
        self.assertEqual(len(rows),2)
        for row in rows:self.assertEqual(set(row),{'question','answer','contexts','ground_truth'})
        self.assertEqual(self.data,before)
        self.assertEqual(self.client.get('/goldstandard/download/historical.json').status_code,200)

    def test_legacy_and_current_sessions_keep_existing_export_default(self):
        for option in ({},{'allow_historical':False},{'allow_historical':True}):
            response=self.client.post('/goldstandard/save',json={'session_id':self.data['session_id'],'filename':'current.json',**option})
            self.assertEqual(response.status_code,200,response.text)
            self.assertFalse(response.json()['historical'])
            self.assertFalse(response.json()['session_validity']['stale'])

    def test_opt_in_requires_actual_boolean_and_does_not_write_on_rejection(self):
        self.data['stale']=True
        for value in (1,0,'true','false',None,[],{}):
            with self.subTest(value=value),patch.object(gs,'_save_export_sync') as write:
                response=self.client.post('/goldstandard/save',json={'session_id':self.data['session_id'],'allow_historical':value})
                self.assertEqual(response.status_code,422,response.text);write.assert_not_called()
        for number in ('NaN','Infinity','-Infinity'):
            response=self.client.post('/goldstandard/save',content=
                '{"session_id":"'+self.data['session_id']+'","allow_historical":'+number+'}',
                headers={'Content-Type':'application/json'})
            self.assertEqual(response.status_code,422,response.text)
            self.assertEqual(response.json()['error']['code'],'INVALID_PARAMETER')
        with self.assertRaises(ValueError):
            asyncio.run(gs.save_session(self.data['session_id'],None,1))

    def test_unknown_session_remains_404(self):
        self.assertEqual(self.client.get('/goldstandard/session/absent').status_code,404)
        self.assertEqual(self.client.post('/goldstandard/save',json={'session_id':'absent','allow_historical':True}).status_code,404)


class ImplementationTests(unittest.TestCase):
    def test_changed_embedded_sources_match_runtime(self):
        root=Path(__file__).resolve().parents[2];text=(root/'IMPLEMENTATION.md').read_text()
        for name in ('api/main.py','api/models/schemas.py','api/routers/goldstandard.py',
                     'api/services/goldstandard.py','ui/src/api/client.ts','ui/src/pages/GoldStandardPage.tsx','scripts/verify/session_validity.py','scripts/verify/10_validity.sh','scripts/verify/05_transfer.sh','scripts/verify/README.md'):
            language='typescript' if name.endswith(('.ts','.tsx')) else 'bash' if name.endswith('.sh') else 'markdown' if name.endswith('.md') else 'python'
            fence='````' if name.endswith('.md') else '```'
            header='### '+name+'\n\n'+fence+language+'\n'
            start=text.index(header)+len(header);end=text.index('\n'+fence+'\n',start)
            with self.subTest(file=name):self.assertEqual(text[start:end],(root/name).read_text().rstrip('\n'))


if __name__=='__main__': unittest.main()
