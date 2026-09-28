"""Real Weaviate reindex with an unreachable embedding endpoint; owned fixtures only."""
import asyncio, os, tempfile, uuid
from pathlib import Path
from unittest.mock import patch
import httpx
from config import settings
from main import app
from services import goldstandard as gs, tuning
from services import weaviate_client as wc


async def completed(client,path,timeout=300):
    deadline=asyncio.get_running_loop().time()+timeout;last='not observed'
    while True:
        remaining=deadline-asyncio.get_running_loop().time()
        if remaining<=0:raise TimeoutError(f'Owned job {path} exceeded {timeout}s; last status={last}')
        try:response=await asyncio.wait_for(client.get(path),remaining)
        except asyncio.TimeoutError as exc:raise TimeoutError(f'Owned job {path} exceeded {timeout}s; last status={last}') from exc
        assert response.status_code==200,response.text
        job=response.json();last=job.get('status','missing')
        if last not in ('queued','running'):
            assert last=='completed',job
            return job
        await asyncio.sleep(min(.1,max(0,deadline-asyncio.get_running_loop().time())))

async def settle_owned_jobs(jobs,timeout=30):
    deadline=asyncio.get_running_loop().time()+timeout
    while True:
        pending=[(module.__name__,jobid,(module.get_job(jobid) or {}).get('status','missing')) for module,jobid in jobs if (module.get_job(jobid) or {}).get('status') not in ('completed','failed')]
        if not pending or asyncio.get_running_loop().time()>=deadline:return pending
        await asyncio.sleep(min(.1,max(0,deadline-asyncio.get_running_loop().time())))

async def bounded_poll_cases():
    from types import SimpleNamespace
    class StuckClient:
        async def get(self,path):return SimpleNamespace(status_code=200,text='',json=lambda:{'status':'running'})
    try:await completed(StuckClient(),'/owned/stuck-job',timeout=.01)
    except TimeoutError as exc:assert '/owned/stuck-job' in str(exc) and 'running' in str(exc)
    else:raise AssertionError('Stuck owned job did not meet its deadline')
    pending=await settle_owned_jobs([(SimpleNamespace(__name__='owned',get_job=lambda identity:{'status':'queued'}),'owned-cleanup-job')],timeout=.01)
    assert pending==[('owned','owned-cleanup-job','queued')]
    print('PASS two controlled job-poll and cleanup deadline cases',flush=True)

async def main():
    token=uuid.uuid4().hex[:8]; name=os.environ.get('RAG_TEST_PREFIX','Vfy49')+'Reindex'+token
    probe=name+'Probe'; sid='gs_'+token; job=None; checks=0
    await bounded_poll_cases()
    client=await asyncio.to_thread(wc.get_client); created=[]
    with tempfile.TemporaryDirectory(prefix='owned-reindex-') as temp, \
         patch.object(settings,'upload_dir',temp), patch.object(settings,'sources_dir',str(Path(temp)/'sources')), \
         patch.object(settings,'ollama_host','127.0.0.1'), patch.object(settings,'ollama_port',1):
        def check(condition,label):
            nonlocal checks
            assert condition,label; checks+=1; print('PASS '+label,flush=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://owned') as api:
            try:
                await asyncio.to_thread(wc._create_collection_sync,probe,'hnsw','cosine',{}); created.append(probe)
                try:
                    await asyncio.to_thread(client.collections.get(probe).data.insert,properties={'content':'Owned endpoint refusal probe'})
                except Exception as exc:
                    message=str(exc).lower()
                    check('connect' in message and ('127.0.0.1:1' in message or 'connection refused' in message),'actual vectorization fails against the closed embedding endpoint')
                else: raise AssertionError('Owned embedding endpoint unexpectedly served a vector')
                check(await asyncio.to_thread(lambda:client.collections.get(probe).aggregate.over_all(total_count=True).total_count)==0,'failed embedding probe stores no object')
                await asyncio.to_thread(wc._create_collection_sync,name,'hnsw','cosine',{}); created.append(name)
                col=client.collections.get(name)
                source=[]
                for i in range(2):
                    identity=str(uuid.uuid4()); vector=[(i+1)/8.0]*768
                    props={'content':'Owned inert reindex '+str(i),'source_file':'owned-inert.txt','source_type':'txt','chunk_index':i,'chunk_strategy':'fixed','chunk_size':150,'chunk_overlap':0,'created_at':'2026-09-28T00:00:00Z'}
                    await asyncio.to_thread(col.data.insert,properties=props,uuid=identity,vector=vector)
                    source.append(identity)
                before=await asyncio.to_thread(tuning._existing_records,name)
                check({r['id'] for r in before}==set(source),'stored explicit vectors and original UUIDs are readable with embeddings unavailable')
                session={'session_id':sid,'collection':name,'status':'completed','pairs_total':1,'pairs_completed':1,'pairs_attempted':1,'pairs_failed':0,'pairs':[{'pair_id':'p_'+token,'question':'Owned question','answer':'Owned answer','ground_truth':'Owned truth','contexts':['Owned inert reindex'],'source_file':'owned-inert.txt','chunk_index':0,'status':'approved'}]}
                await asyncio.to_thread(gs.store_session,session); session_bytes=await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes())
                request=await api.post('/tune/reindex',json={'collection':name,'index_type':'flat','distance_metric':'dot'})
                check(request.status_code==202,'real reindex HTTP handler queues the job with closed embedding configuration'); job=request.json()['job_id']
                result=await completed(api,'/tune/job/'+job)
                check(result['status']=='completed' and result['chunks_written']==len(before), 'job completes only after final backend verification: '+str(result))
                after=await asyncio.to_thread(tuning._existing_records,name)
                check({r['id']:r for r in after}=={r['id']:r for r in before},'UUIDs, every property and all stored vector values match exactly after reindex')
                config=await asyncio.to_thread(wc._collection_config_sync,name)
                check(config['index_type']=='flat' and config['distance_metric']=='dot','physical index and distance change to flat/dot')
                check(await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes()==session_bytes and not gs.get_session(sid).get('stale')),'retained evaluation identity/content/validity are unchanged')
                check(any('verified unchanged' in note for note in result['notes']),'completion notes truthfully report verified identity and vector preservation')
            finally:
                if job:
                    pending=await settle_owned_jobs([(tuning,job)])
                    if pending:
                        print(f'FAIL owned jobs still active: {pending}; preserving fixtures {created} and {temp}',flush=True)
                        # Standalone verifier: preserve active fixtures and avoid executor join.
                        os._exit(2)
                for owned in reversed(created):
                    if await asyncio.to_thread(client.collections.exists,owned): await asyncio.to_thread(client.collections.delete,owned)
                gs._sessions.pop(sid,None)
    await asyncio.to_thread(client.close); print(str(checks)+' real reindex checks passed',flush=True)

asyncio.run(main())
