"""Owned real HTTP/package/backend export-edit-rename-import-twice acceptance.

Pairs and vectors are synthetic; package/model metadata and import/export jobs
are real. No generation call or startup sweep runs in this process.
"""
import asyncio,copy,hashlib,json,os,subprocess,sys,tarfile,tempfile,uuid
from pathlib import Path
from unittest.mock import patch
import httpx
from config import settings
from main import app
from services import exporter,goldstandard as gs,importer,weaviate_client as wc

collection=os.environ.get('RAG_TEST_PREFIX','Vfy')+'Identity'+uuid.uuid4().hex[:10]
sid='gs_'+uuid.uuid4().hex[:8]
created=False
jobs=[]
with tempfile.TemporaryDirectory(prefix='owned-import-session-') as directory:
    root=Path(directory)
    with patch.object(settings,'upload_dir',str(root/'uploads')),patch.object(settings,'sources_dir',str(root/'sources')),patch.object(settings,'exports_dir',str(root/'exports')),patch.object(gs,'_sessions',{}):
        async def completed(client,path):
            while True:
                response=await client.get(path);assert response.status_code==200,response.text
                job=response.json()
                if job['status'] not in ('queued','running'):
                    assert job['status']=='completed',job
                    return job
                await asyncio.sleep(0.1)

        async def run():
            global created
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://owned-fixture') as client:
                assert not wc._collection_exists_sync(collection),'Owned name already exists'
                created=True
                response=await client.post('/collections',json={'name':collection});assert response.status_code==201,response.text
                coll=wc.get_client().collections.get(collection)
                coll.data.insert(properties={'content':'Owned inert evaluation context','source_file':'inert.txt','chunk_index':0},vector=[0.1]*768)
                session={'session_id':sid,'collection':collection,'status':'completed','pairs_total':1,'pairs_completed':1,'pairs':[{'pair_id':'p_owned','question':'Inert question','answer':'Original exported answer','contexts':['Owned inert evaluation context'],'ground_truth':'Inert truth','source_file':'inert.txt','chunk_index':0,'status':'approved'}]}
                gs.store_session(session)
                print('PASS owned real collection, supplied-vector object and synthetic evaluation session created',flush=True)
                start=await client.post('/export',json={'collection':collection,'include_models':False});assert start.status_code==202,start.text
                jobs.append((exporter,start.json()['job_id']))
                exported=await completed(client,'/export/job/'+start.json()['job_id']);archive=root/'exports'/exported['filename'];digest=hashlib.sha256(archive.read_bytes()).hexdigest()
                with tarfile.open(archive) as package:
                    member=next(m for m in package.getmembers() if m.name.endswith('/goldstandard/'+sid+'.json'))
                    retained=json.load(package.extractfile(member));assert retained['pairs'][0]['answer']=='Original exported answer'
                print('PASS actual completed export package contains the original session snapshot',flush=True)
                edited=await client.patch('/goldstandard/session/'+sid+'/pair/p_owned',json={'status':'edited','answer':'Newer acknowledged human answer'});assert edited.status_code==200,edited.text
                original_path=gs._session_path(sid);original_bytes=original_path.read_bytes()
                print('PASS newer original human edit acknowledged through HTTP after package export',flush=True)
                mappings=[];targets=[]
                for _ in range(2):
                    start=await client.post('/import',json={'filename':exported['filename'],'on_conflict':'rename'});assert start.status_code==202,start.text
                    jobs.append((importer,start.json()['job_id']))
                    imported=await completed(client,'/import/job/'+start.json()['job_id'])
                    assert imported['renamed'] and len(imported['restored_sessions'])==1,imported
                    mapping=imported['restored_sessions'][0];assert mapping['source_session_id']==sid and mapping['collection']==imported['collection']
                    assert any(mapping['session_id'] in note for note in imported['notes']),imported
                    mappings.append(mapping);targets.append(imported['collection'])
                    assert original_path.read_bytes()==original_bytes,'Original human edit was overwritten'
                assert len({collection,*targets})==3 and len({sid,*[m['session_id'] for m in mappings]})==3
                print('PASS two actual rename imports expose distinct collections and independent local session mappings without changing original bytes',flush=True)
                for local_sid,target,expected in [(sid,collection,'Newer acknowledged human answer')]+[(m['session_id'],m['collection'],'Original exported answer') for m in mappings]:
                    response=await client.get('/goldstandard/session/'+local_sid);assert response.status_code==200,response.text
                    loaded=response.json();assert loaded['collection']==target and loaded['pairs'][0]['answer']==expected,loaded
                    if local_sid!=sid:
                        assert loaded['imported_from']['session_id']==sid and loaded['imported_from']['collection']==collection and loaded['imported_from']['imported_at']
                    saved=await client.post('/goldstandard/save',json={'session_id':local_sid,'filename':'owned-'+local_sid+'.json'});assert saved.status_code==200,saved.text
                    download=await client.get('/goldstandard/download/'+saved.json()['filename']);assert download.status_code==200,download.text
                    rows=download.json();assert len(rows)==1 and rows[0]['answer']==expected and set(rows[0])=={'question','answer','contexts','ground_truth'},rows
                print('PASS original and both reported imported IDs remain usable through HTTP lookup/provenance/save/download with exact four-field RAGAS rows',flush=True)
                code="from config import settings;from services import goldstandard as gs;import json,sys;settings.upload_dir=sys.argv[1];gs.load_sessions_from_disk();print(json.dumps([gs.get_session(s) for s in sys.argv[2:]]))"
                identities=[sid]+[m['session_id'] for m in mappings]
                reload=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',code,str(root/'uploads'),*identities],text=True,capture_output=True,check=True)
                fresh=json.loads(reload.stdout);assert [row['collection'] for row in fresh]==[collection,*targets];assert fresh[0]['pairs'][0]['answer']=='Newer acknowledged human answer';assert all(row['imported_from']['session_id']==sid for row in fresh[1:])
                print('PASS fresh independent API process restores all three session identities, newer original review and imported provenance',flush=True)
                start=await client.post('/export',json={'collection':targets[0],'include_models':False});assert start.status_code==202,start.text
                jobs.append((exporter,start.json()['job_id']))
                reexported=await completed(client,'/export/job/'+start.json()['job_id'])
                with tarfile.open(root/'exports'/reexported['filename']) as package:
                    imported_sid=mappings[0]['session_id'];member=next(m for m in package.getmembers() if m.name.endswith('/goldstandard/'+imported_sid+'.json'))
                    retained=json.load(package.extractfile(member));assert retained['session_id']==imported_sid and retained['imported_from']['session_id']==sid
                assert hashlib.sha256(archive.read_bytes()).hexdigest()==digest
                print('PASS re-export uses the allocated session filename/provenance and the original package remains byte-identical',flush=True)

        async def owned():
            try:await run()
            finally:
                # Let only our actual jobs reach terminal state before releasing
                # temporary settings/storage or deleting their owned collections.
                while any(module.get_job(jobid)['status'] in ('queued','running') for module,jobid in jobs):await asyncio.sleep(0.1)
                if created:
                    for name in wc.get_client().collections.list_all():
                        if name==collection or name.startswith(collection+'_'):
                            wc._delete_collection_sync(name)
                wc.close_client()
        asyncio.run(owned())
assert not wc._collection_exists_sync(collection);wc.close_client()
print('PASS only owned collections/packages/session fixtures removed',flush=True)
