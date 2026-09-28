"""Owned import identity preservation; run by13_identity.sh in the API image."""
import asyncio,copy,json,os,sys,tempfile,threading,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR',str(Path(__file__).resolve().parents[2]/'api') if __file__!='<stdin>' else '/app'))
from config import settings
from services import goldstandard as gs,importer


def fixture():
    return {'session_id':'gs_460abcde','collection':'OwnedOriginal','status':'completed','pairs_total':1,'pairs_completed':1,'pairs':[{'pair_id':'p_owned','question':'Inert question','answer':'Original answer','contexts':['Inert context'],'ground_truth':'Inert truth','source_file':'inert.txt','chunk_index':0,'status':'approved'}]}


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        for obj,key,value in [(settings,'upload_dir',self.tmp.name),(settings,'sources_dir',str(Path(self.tmp.name)/'sources')),(gs,'_sessions',{})]:
            change=patch.object(obj,key,value);change.start();self.addCleanup(change.stop)
        self.original=fixture();gs.store_session(copy.deepcopy(self.original));self.path=gs._session_path(self.original['session_id'])

    def test_restore_twice_preserves_newer_original_review_and_independent_exports(self):
        package=Path(self.tmp.name)/'owned-package';gold=package/'goldstandard';gold.mkdir(parents=True)
        (gold/(self.original['session_id']+'.json')).write_text(json.dumps(self.original))
        asyncio.run(gs.update_pair(self.original['session_id'],'p_owned',{'answer':'Newer human review','status':'edited'}));before=self.path.read_bytes()
        imported=[]
        for target in ['OwnedRenamedOne','OwnedRenamedTwo']:
            mappings=[];notes=importer._restore_sidecars(target,package,'OwnedOriginal',mappings)
            self.assertEqual(len(mappings),1);self.assertTrue(any(mappings[0]['session_id'] in note for note in notes))
            session=gs.get_session(mappings[0]['session_id']);imported.append(session)
            self.assertEqual(session['imported_from']['session_id'],self.original['session_id']);self.assertEqual(session['imported_from']['collection'],'OwnedOriginal')
        identities=[self.original['session_id']]+[s['session_id'] for s in imported];self.assertEqual(len(set(identities)),3);self.assertEqual(self.path.read_bytes(),before)
        for i,sid in enumerate(identities):
            result=asyncio.run(gs.save_session(sid,'owned-export'+str(i)+'.json'));rows=json.loads((Path(self.tmp.name)/result['filename']).read_text())
            self.assertEqual(rows[0]['answer'],'Newer human review' if i==0 else 'Original answer');self.assertEqual(set(rows[0]),{'question','answer','contexts','ground_truth'})
        gs._sessions={};gs.load_sessions_from_disk();self.assertEqual({gs.get_session(sid)['collection'] for sid in identities},{'OwnedOriginal','OwnedRenamedOne','OwnedRenamedTwo'})

    def test_concurrent_imports_select_distinct_local_identities(self):
        before=self.path.read_bytes()
        def run(i):
            data=fixture();data['collection']='OwnedImported'+str(i)
            return gs.store_imported_session(data,'OwnedOriginal')['session_id']
        with ThreadPoolExecutor(max_workers=8) as pool:identities=list(pool.map(run,range(16)))
        self.assertEqual(len(set(identities)),16);self.assertNotIn(self.original['session_id'],identities);self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(len(list(self.path.parent.glob('*.json'))),17)

    def test_cold_cache_still_respects_existing_disk_identity(self):
        before=self.path.read_bytes();gs._sessions={}
        result=gs.store_imported_session(fixture(),'OwnedOriginal')
        self.assertNotEqual(result['session_id'],self.original['session_id']);self.assertEqual(self.path.read_bytes(),before)

    def test_unreadable_original_bytes_still_occupy_the_identity(self):
        self.path.write_bytes(b'{owned retained unreadable bytes');gs._sessions={}
        result=gs.store_imported_session(fixture(),'OwnedOriginal')
        self.assertNotEqual(result['session_id'],self.original['session_id']);self.assertEqual(self.path.read_bytes(),b'{owned retained unreadable bytes')

    def test_collision_exhaustion_is_bounded_without_overwrite(self):
        before=self.path.read_bytes()
        with patch.object(gs.uuid,'uuid4',return_value=SimpleNamespace(hex='460abcde'+'0'*24)) as ids:
            with self.assertRaisesRegex(RuntimeError,'unoccupied session'):gs.store_imported_session(fixture(),'OwnedOriginal')
        self.assertEqual(ids.call_count,128);self.assertEqual(self.path.read_bytes(),before);self.assertEqual(len(gs._sessions),1)

    def test_redirected_existing_identity_is_reserved_without_following_it(self):
        foreign=Path(self.tmp.name)/'owned-retained-neighbor';foreign.write_bytes(b'owned retained bytes')
        self.path.unlink();self.path.symlink_to(foreign);gs._sessions={}
        saved=gs.store_imported_session(fixture(),'OwnedOriginal')
        self.assertNotEqual(saved['session_id'],self.original['session_id']);self.assertTrue(self.path.is_symlink())
        self.assertEqual(foreign.read_bytes(),b'owned retained bytes')

    def test_free_valid_source_identity_is_retained_with_provenance(self):
        data=fixture();data['session_id']='gs_460abcdf';data['collection']='OwnedNew'
        result=gs.store_imported_session(data,'ExternalOriginal')
        self.assertEqual(result['session_id'],data['session_id']);self.assertEqual(result['imported_from']['collection'],'ExternalOriginal')
        self.assertTrue(result['imported_from']['imported_at'].endswith('+00:00'))

    def test_inputs_and_returned_snapshots_do_not_alias_imported_storage(self):
        data=fixture();before=copy.deepcopy(data);result=gs.store_imported_session(data,'OwnedOriginal')
        result['pairs'][0]['answer']='Returned mutation';data['pairs'][0]['answer']='Caller mutation'
        self.assertEqual(gs.get_session(result['session_id'])['pairs'][0]['answer'],'Original answer');self.assertNotIn('imported_from',before)

    def test_storage_inspection_failure_never_guesses_a_free_slot(self):
        before=self.path.read_bytes()
        with patch.object(Path,'lstat',side_effect=PermissionError('Owned identity inspection failure')):
            with self.assertRaises(PermissionError):gs.store_imported_session(fixture(),'OwnedOriginal')
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(len(gs._sessions),1)

    def test_generation_start_uses_the_same_namespace_without_overwriting_collision(self):
        before=self.path.read_bytes()
        async def run():
            with patch.object(gs.wc,'sample_chunks',new=AsyncMock(return_value=[])),patch.object(gs,'_run_generation',new=AsyncMock()),patch.object(gs.uuid,'uuid4',side_effect=[SimpleNamespace(hex='460abcde'+'0'*24),SimpleNamespace(hex='460abcdf'+'0'*24)]):
                result=await gs.start_generation('OwnedGeneration',1,None)
                await asyncio.gather(*list(gs._tasks))
            self.assertEqual(result['session_id'],'gs_460abcdf')
        asyncio.run(run());self.assertEqual(self.path.read_bytes(),before)

    def test_concurrent_generated_and_imported_sessions_share_identity_serialization(self):
        before=self.path.read_bytes()
        def run(i):
            data=fixture();data['collection']='OwnedCreated'+str(i)
            saved=(gs.store_imported_session(data,'OwnedOriginal') if i%2 else gs._store_generated_session(data))
            return saved['session_id']
        with ThreadPoolExecutor(max_workers=8) as pool:identities=list(pool.map(run,range(16)))
        self.assertEqual(len(set(identities)),16);self.assertNotIn(self.original['session_id'],identities);self.assertEqual(self.path.read_bytes(),before)

    def test_historical_source_identity_gets_safe_local_id_and_usable_provenance(self):
        from models.schemas import SessionResponse
        package=Path(self.tmp.name)/'legacy-package';gold=package/'goldstandard';gold.mkdir(parents=True)
        data=fixture();data['session_id']='legacy-review-2024';(gold/'legacy.json').write_text(json.dumps(data))
        mappings=[];importer._restore_sidecars('OwnedLegacy',package,'OwnedOriginal',mappings)
        local=mappings[0]['session_id'];self.assertRegex(local,r'^gs_[0-9a-f]{8}$')
        loaded=gs.get_session(local);self.assertEqual(loaded['imported_from']['session_id'],data['session_id'])
        self.assertEqual(SessionResponse.model_validate(loaded).imported_from.session_id,data['session_id'])
        self.assertFalse((gs._sessions_dir()/'legacy-review-2024.json').exists())
        result=asyncio.run(gs.save_session(local,'legacy-rows.json'));self.assertEqual(json.loads((Path(self.tmp.name)/result['filename']).read_text())[0]['answer'],'Original answer')

    def test_cache_iteration_serializes_with_generation_insertion(self):
        entered=threading.Event();release=threading.Event();started=threading.Event();mutated=threading.Event()
        class PausedCache(dict):
            def __setitem__(cache,key,value):
                super(PausedCache,cache).__setitem__(key,value);mutated.set()
            def items(cache):
                iterator=iter(super(PausedCache,cache).items())
                entered.set();self.assertTrue(release.wait(2),'Cache fixture was not released')
                return iterator
        gs._sessions=PausedCache(gs._sessions)
        with ThreadPoolExecutor(max_workers=2) as pool:
            reading=pool.submit(gs.sessions_for,'OwnedOriginal');self.assertTrue(entered.wait(2))
            def create():
                started.set();return gs._store_generated_session(fixture())
            writing=pool.submit(create);self.assertTrue(started.wait(2))
            try:self.assertFalse(mutated.wait(.1),'Insertion bypassed the cache snapshot lock')
            finally:release.set()
            self.assertEqual(len(reading.result(timeout=2)),1);self.assertRegex(writing.result(timeout=2)['session_id'],r'^gs_[0-9a-f]{8}$')


if __name__=='__main__':unittest.main()
