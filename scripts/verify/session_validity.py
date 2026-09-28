"""Real backend/session marking and in-process HTTP validity/export acceptance.

Run inside a disposable API: python - < scripts/verify/session_validity.py
Uses a unique real collection, synthetic pairs and temporary local files. No
startup sweep or model calls. Only its own collection/session/files are removed.
"""
import copy
import os
import tempfile
import uuid
from unittest.mock import patch
from fastapi.testclient import TestClient
from config import settings
from main import app
from services import goldstandard as gs, weaviate_client as wc

collection=os.environ.get('RAG_TEST_PREFIX','Vfy')+'Validity'+uuid.uuid4().hex[:12]
session_id='gs_'+uuid.uuid4().hex[:8]
client=TestClient(app)  # Do not run global startup sweeps alongside other tests.
creation_attempted=False
with tempfile.TemporaryDirectory(prefix='validity-live-') as directory, patch.object(settings,'upload_dir',directory):
    try:
        creation_attempted=True
        response=client.post('/collections',json={'name':collection})
        assert response.status_code==201,response.text
        print('PASS unique real backend collection created',flush=True)
        pairs=[{'pair_id':'validity_pair','question':'Inert question','answer':'Inert answer',
            'contexts':['Inert retained context'],'ground_truth':'Inert truth','source_file':'inert.txt',
            'chunk_index':0,'status':'approved'}]
        session={'session_id':session_id,'collection':collection,'status':'completed',
            'pairs_total':1,'pairs_completed':1,'pairs':pairs}
        gs.store_session(session)
        response=client.get('/goldstandard/session/'+session_id)
        assert response.status_code==200,response.text
        assert not response.json()['stale'] and not response.json()['orphaned']
        assert response.json()['pairs']==pairs
        print('PASS legacy defaults and retained pairs reach HTTP response',flush=True)
        assert gs.mark_stale(collection,'Synthetic chunk-identity change')==1
        response=client.get('/goldstandard/session/'+session_id)
        assert response.json()['stale'] and response.json()['stale_reason']=='Synthetic chunk-identity change'
        assert response.json()['stale_at']
        print('PASS real persistence marker reason/timestamp reach HTTP response',flush=True)
        response=client.post('/goldstandard/save',json={'session_id':session_id})
        assert response.status_code==409 and response.json()['error']['code']=='HISTORICAL_SESSION',response.text
        print('PASS stale export is refused without explicit choice',flush=True)
        response=client.delete('/collections/'+collection+'?confirm=true')
        assert response.status_code==200,response.text
        creation_attempted=False
        response=client.get('/goldstandard/session/'+session_id)
        historical=response.json()
        assert response.status_code==200 and historical['orphaned'] and historical['orphaned_reason'] and historical['orphaned_at']
        assert historical['stale'] and historical['pairs']==pairs
        print('PASS actual collection deletion forwards orphan warning without deleting history',flush=True)
        before=copy.deepcopy(gs.get_session(session_id))
        response=client.post('/goldstandard/save',json={'session_id':session_id,'allow_historical':True,'filename':'historical.json'})
        assert response.status_code==200,response.text
        exported=response.json()
        assert exported['historical'] and exported['session_validity']['stale'] and exported['session_validity']['orphaned']
        download=client.get('/goldstandard/download/'+exported['filename'])
        assert download.status_code==200,download.text
        rows=download.json()
        assert len(rows)==1 and set(rows[0])=={'question','answer','contexts','ground_truth'}
        assert gs.get_session(session_id)==before
        print('PASS explicit historical export retains RAGAS fields and original history',flush=True)
    finally:
        if creation_attempted and wc._collection_exists_sync(collection):
            response=client.delete('/collections/'+collection+'?confirm=true')
            assert response.status_code==200,response.text
        gs._sessions.pop(session_id,None)
        wc.close_client()
assert not wc._collection_exists_sync(collection)
wc.close_client()
print('PASS owned collection/session/files removed',flush=True)
