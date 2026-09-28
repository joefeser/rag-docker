"""Durable acknowledged mutations and controlled failure/interleaving acceptance."""
import asyncio,json,os,sys,tempfile,threading,unittest,subprocess,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock,patch
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR',str(Path(__file__).resolve().parents[2]/'api')))
from config import settings
from services import goldstandard as gs


def fixture():
    return {'session_id':'gs_450abcde','collection':'OwnedPersistence','status':'completed','pairs_total':2,'pairs_completed':2,'pairs':[{'pair_id':'p_'+str(i),'question':'Original','answer':'Original','contexts':['Inert'],'ground_truth':'Original','source_file':'inert.txt','chunk_index':i,'status':'pending'} for i in range(2)]}

class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        for obj,key,value in [(settings,'upload_dir',self.tmp.name),(gs,'_sessions',{}),(gs,'_diagnostics',{})]:
            change=patch.object(obj,key,value);change.start();self.addCleanup(change.stop)
        self.data=fixture();gs.store_session(self.data)

    def restart(self):
        gs._sessions={};gs.load_sessions_from_disk();return gs.get_session(self.data['session_id'])

    def test_parallel_acknowledged_fields_survive_restart(self):
        data=fixture();data["pairs"]=[{**data["pairs"][0],"pair_id":"p_"+str(i)} for i in range(8)];data.update(pairs_total=8,pairs_completed=8);gs.store_session(data)
        barrier=threading.Barrier(16)
        def edit(i):
            barrier.wait();return asyncio.run(gs.update_pair(self.data['session_id'],'p_'+str(i//2),{('question' if i%2==0 else 'answer'):str(i)}))
        with ThreadPoolExecutor(max_workers=16) as pool:results=list(pool.map(edit,range(16)))
        self.assertTrue(all(results));state=self.restart()
        for i in range(16):self.assertEqual(state['pairs'][i//2]['question' if i%2==0 else 'answer'],str(i))

    def test_replace_failure_leaves_previous_snapshot_and_reports_failure(self):
        path=gs._session_path(self.data['session_id']);before=path.read_bytes()
        with patch.object(gs.os,'replace',side_effect=OSError('Controlled replace fault')):
            with self.assertRaises(gs.GoldStandardError) as error:asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Failed edit'}))
        self.assertEqual(error.exception.code,'SESSION_WRITE_FAILED')
        self.assertEqual(path.read_bytes(),before);self.assertEqual(gs.get_session(self.data['session_id']),self.data)
        self.assertFalse(list(path.parent.glob('*.tmp')))
        self.assertEqual(gs.session_diagnostics()[0]['code'],'SESSION_WRITE_FAILED')
        asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Accepted edit'}));self.assertEqual(gs.session_diagnostics(),[])

    def test_file_fsync_failure_leaves_old_snapshot(self):
        path=gs._session_path(self.data['session_id']);before=path.read_bytes()
        with patch.object(gs.os,'fsync',side_effect=OSError('Controlled file fsync')):
            with self.assertRaises(gs.GoldStandardError):asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Not accepted'}))
        self.assertEqual(path.read_bytes(),before);self.assertEqual(gs.get_session(self.data['session_id']),self.data)

    def test_after_replace_failure_is_uncertain_and_cache_matches_disk(self):
        with patch.object(gs,'_sync_directory',side_effect=OSError('Controlled directory fsync')):
            with self.assertRaises(gs.GoldStandardError) as error:asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Replaced'}))
        self.assertEqual(error.exception.code,'SESSION_DURABILITY_UNCERTAIN')
        self.assertEqual(json.loads(gs._session_path(self.data['session_id']).read_text()),gs.get_session(self.data['session_id']))
        self.assertEqual(gs.get_session(self.data['session_id'])['pairs'][0]['answer'],'Replaced')

    def test_unreadable_and_invalid_files_are_preserved_reported(self):
        files={'gs_450bad00.json':'{incomplete','gs_450bad01.json':'[]','gs_450bad02.json':'{"session_id":"gs_450bad02","pairs":[]}'}
        for name,text in files.items():(gs._sessions_dir()/name).write_text(text)
        self.assertEqual(len(gs.session_diagnostics()),3)
        for name,text in files.items():self.assertEqual((gs._sessions_dir()/name).read_text(),text)
        self.assertEqual(len(gs.sessions_for('OwnedPersistence')),1)

    def test_no_mutable_aliases_from_storage_reads_or_exports(self):
        self.data['pairs'][0]['answer']='Caller mutation'
        gs.get_session(self.data['session_id'])['pairs'][0]['answer']='Reader mutation'
        gs.sessions_for('OwnedPersistence')[0]['pairs'].clear()
        self.assertEqual(self.restart()['pairs'][0]['answer'],'Original')

    def test_generation_review_flags_interleave_without_lost_updates(self):
        async def run():
            pending=asyncio.Event();release=asyncio.Event()
            initial=fixture();initial.update(status='generating',pairs_total=3);gs.store_session(initial)
            async def pair(chunk):pending.set();await release.wait();return {**initial['pairs'][0],'pair_id':'p_generated'}
            with patch.object(gs,'_generate_pair',side_effect=pair):
                task=asyncio.create_task(gs._run_generation(initial['session_id'],[{'content':'Inert'}]));await pending.wait()
                await asyncio.gather(gs.update_pair(initial['session_id'],'p_0',{'answer':'Reviewed answer'}),gs.update_pair(initial['session_id'],'p_1',{'question':'Reviewed question'}))
                await asyncio.to_thread(gs.mark_stale,'OwnedPersistence','Controlled identity change')
                release.set();await task
        asyncio.run(run());state=self.restart();self.assertEqual(state['status'],'completed');self.assertEqual(len(state['pairs']),3)
        self.assertEqual(state['pairs'][0]['answer'],'Reviewed answer');self.assertEqual(state['pairs'][1]['question'],'Reviewed question');self.assertTrue(state['stale']);self.assertEqual(state['pairs_attempted'],1)

    def test_regeneration_rejects_changed_target_preserving_acknowledged_edit(self):
        async def run():
            pending=asyncio.Event();release=asyncio.Event()
            async def pair(chunk):pending.set();await release.wait();return {**fixture()['pairs'][0],'answer':'Generated replacement'}
            with patch.object(gs,'_generate_pair',side_effect=pair):
                task=asyncio.create_task(gs.regenerate_pair(self.data['session_id'],'p_0'));await pending.wait()
                await gs.update_pair(self.data['session_id'],'p_0',{'answer':'Acknowledged review'});release.set()
                with self.assertRaises(gs.GoldStandardError) as error:await task
                self.assertEqual(error.exception.code,'PAIR_CHANGED_DURING_REGENERATION')
        asyncio.run(run());self.assertEqual(self.restart()['pairs'][0]['answer'],'Acknowledged review')

    def test_regeneration_preserves_other_pair_edits_and_history_flags(self):
        async def run():
            pending=asyncio.Event();release=asyncio.Event()
            async def pair(chunk):pending.set();await release.wait();return {**fixture()['pairs'][0],'answer':'Generated replacement'}
            with patch.object(gs,'_generate_pair',side_effect=pair):
                task=asyncio.create_task(gs.regenerate_pair(self.data['session_id'],'p_0'));await pending.wait()
                await gs.update_pair(self.data['session_id'],'p_1',{'answer':'Other review'})
                gs.mark_orphaned('OwnedPersistence','Controlled deletion');release.set();await task
        asyncio.run(run());state=self.restart();self.assertTrue(state['orphaned']);self.assertEqual([p['answer'] for p in state['pairs']],['Generated replacement','Other review'])

    def test_hard_killed_writer_preserves_valid_snapshot_reports_owned_temporary(self):
        path=gs._session_path(self.data['session_id']);before=path.read_bytes();marker=Path(self.tmp.name)/'replace-boundary'
        code="""import sys,time,asyncio
from pathlib import Path
from config import settings
from services import goldstandard as gs
settings.upload_dir=sys.argv[1]
gs.load_sessions_from_disk()
def stopped(src,dst):
    Path(sys.argv[2]).write_text('ready')
    time.sleep(30)
gs.os.replace=stopped
asyncio.run(gs.update_pair('gs_450abcde','p_0',{'answer':'Interrupted edit'}))
"""
        env={**os.environ,'PYTHONPATH':os.environ.get('RAG_TEST_API_DIR',str(Path(__file__).resolve().parents[2]/'api'))}
        child=subprocess.Popen([sys.executable,'-c',code,self.tmp.name,str(marker)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5
            while not marker.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(0.02)
            self.assertTrue(marker.exists(),'Owned child did not reach replace boundary')
            child.kill();child.communicate(timeout=5)
            self.assertEqual(path.read_bytes(),before);self.assertEqual(self.restart(),self.data)
            issues=gs.session_diagnostics();self.assertEqual(len(issues),1);self.assertEqual(issues[0]['code'],'SESSION_INTERRUPTED_WRITE')
            self.assertTrue(list(path.parent.glob('.gs_450abcde-*.tmp')))
        finally:
            if child.poll() is None:child.kill();child.communicate(timeout=5)

    def test_export_keeps_captured_snapshot_while_new_edit_commits(self):
        initial=fixture();initial['pairs'][0]['status']='approved';gs.store_session(initial)
        started=threading.Event();release=threading.Event();original=gs._save_export_sync
        def delayed(path,rows):
            started.set()
            if not release.wait(5):raise AssertionError('Owned export was not released')
            original(path,rows)
        async def run():
            with patch.object(gs,'_save_export_sync',side_effect=delayed):
                task=asyncio.create_task(gs.save_session(initial['session_id'],'snapshot.json'))
                self.assertTrue(await asyncio.to_thread(started.wait,5))
                try:await gs.update_pair(initial['session_id'],'p_0',{'answer':'Later acknowledged edit'})
                finally:release.set()
                result=await task;self.assertEqual(result['pairs_saved'],1)
        asyncio.run(run())
        self.assertEqual(json.loads((Path(self.tmp.name)/'snapshot.json').read_text())[0]['answer'],'Original')
        self.assertEqual(self.restart()['pairs'][0]['answer'],'Later acknowledged edit')

    def test_scan_of_missing_storage_does_not_create_directory(self):
        with tempfile.TemporaryDirectory() as missing:
            with patch.object(settings,'upload_dir',missing):
                self.assertEqual(gs.session_diagnostics(),[])
                self.assertFalse((Path(missing)/'goldstandard_sessions').exists())

    def test_storage_scan_failure_reports_without_destroying_cached_state(self):
        before=gs.get_session(self.data['session_id'])
        with patch.object(gs.os,'scandir',side_effect=OSError('Owned storage read failure')):
            gs.load_sessions_from_disk()
            self.assertEqual(gs.session_diagnostics()[0]['code'],'SESSION_STORAGE_UNAVAILABLE')
        self.assertEqual(gs.get_session(self.data['session_id']),before)
        self.assertEqual(gs.session_diagnostics(),[])

    def test_model_failure_and_cancellation_status_are_persisted(self):
        async def run():
            initial=fixture();initial.update(status='generating',pairs=[],pairs_total=1,pairs_completed=0);gs.store_session(initial)
            with patch.object(gs,'_generate_pair',new=AsyncMock(side_effect=ValueError('Controlled model failure'))):await gs._run_generation(initial['session_id'],[{}])
            self.assertEqual(gs.get_session(initial['session_id'])['status'],'failed')
            initial['status']='generating';gs.store_session(initial);pending=asyncio.Event()
            async def wait(chunk):pending.set();await asyncio.Event().wait()
            with patch.object(gs,'_generate_pair',side_effect=wait):
                task=asyncio.create_task(gs._run_generation(initial['session_id'],[{}]));await pending.wait();task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
        asyncio.run(run());self.assertEqual(self.restart()['status'],'cancelled')

class DocumentationTests(unittest.TestCase):
    def test_changed_embedded_sources_match_runtime(self):
        root=Path(__file__).resolve().parents[2];text=(root/'IMPLEMENTATION.md').read_text()
        names=['api/services/goldstandard.py', 'api/routers/goldstandard.py', 'ui/src/api/client.ts', 'ui/src/pages/HealthPage.tsx', 'scripts/verify/04_goldstandard.sh', 'scripts/verify/12_persistence.sh', 'scripts/verify/session_persistence.py', 'scripts/verify/README.md', 'scripts/verify/browser/ui_criteria.js']
        for name in names:
            lang='typescript' if name.endswith(('.ts','.tsx')) else 'bash' if name.endswith('.sh') else 'markdown' if name.endswith('.md') else 'javascript' if name.endswith('.js') else 'python';fence='````' if name.endswith('.md') else '```';header='### '+name+'\n\n'+fence+lang+'\n'
            a=text.index(header)+len(header);b=text.index('\n'+fence+'\n',a)
            with self.subTest(file=name):self.assertEqual(text[a:b],(root/name).read_text().rstrip('\n'))

if __name__=='__main__':unittest.main()
