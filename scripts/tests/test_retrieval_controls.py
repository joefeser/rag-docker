"""Observed physical-index reporting and executed query-method controls."""
import asyncio,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock,patch
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR',str(Path(__file__).resolve().parents[2]/'api')))
from config import settings
from services import weaviate_client as wc,rag_pipeline as rag,retrieval_config as saved
from weaviate.classes.config import VectorDistances

class ReportingTests(unittest.TestCase):
    def test_unrecognized_index_does_not_break_collection_listing(self):
        dynamic = type('ObservedDynamic', (), {'distance_metric': VectorDistances.COSINE})()
        collections = {}
        for name, config in [('NamedVectors', None), ('Dynamic', dynamic)]:
            coll = MagicMock()
            coll.config.get.return_value = SimpleNamespace(vector_index_config=config)
            coll.aggregate.over_all.return_value = SimpleNamespace(total_count=1)
            collections[name] = coll
        client = MagicMock()
        client.collections.list_all.return_value = collections
        client.collections.get.side_effect = collections.__getitem__
        with patch.object(wc, 'get_client', return_value=client):
            rows = wc._get_collections_sync()
        self.assertEqual([row['name'] for row in rows], ['NamedVectors', 'Dynamic'])
        self.assertEqual([row['index_type'] for row in rows], ['unknown', 'dynamic'])
        self.assertEqual([row['distance_metric'] for row in rows], ['unknown', 'cosine'])
        self.assertTrue(all(row['hnsw_config'] is None for row in rows))

    def test_backend_values_reach_list_without_default_substitution(self):
        hnsw=type('ObservedHNSW',(),{'ef':-1,'ef_construction':1000,'max_connections':256,'distance_metric':VectorDistances.DOT})()
        flat=type('ObservedFlat',(),{'distance_metric':VectorDistances.L2_SQUARED})()
        collections={}
        for name,config in [('SyntheticHnsw',hnsw),('SyntheticFlat',flat)]:
            coll=MagicMock();coll.config.get.return_value=SimpleNamespace(vector_index_config=config)
            coll.aggregate.over_all.return_value=SimpleNamespace(total_count=3);collections[name]=coll
        client=MagicMock();client.collections.list_all.return_value=collections;client.collections.get.side_effect=collections.__getitem__
        with patch.object(wc,'get_client',return_value=client):rows=wc._get_collections_sync()
        self.assertEqual(rows[0]['hnsw_config'],{'ef':-1,'efConstruction':1000,'maxConnections':256})
        self.assertEqual(rows[0]['distance_metric'],'dot');self.assertEqual(rows[1]['index_type'],'flat')
        self.assertEqual(rows[1]['distance_metric'],'l2-squared');self.assertIsNone(rows[1]['hnsw_config'])


class BackendControlTests(unittest.TestCase):
    def test_hybrid_weight_and_semantic_top_k_reach_supported_sdk_calls(self):
        client=MagicMock();coll=MagicMock();client.collections.get.return_value=coll
        coll.query.hybrid.return_value=SimpleNamespace(objects=[])
        coll.query.near_text.return_value=SimpleNamespace(objects=[])
        with patch.object(wc,'get_client',return_value=client):
            self.assertEqual(wc._hybrid_query_sync('Synthetic','inert query',0.25,7),[])
            self.assertEqual(wc._near_text_query_sync('Synthetic','inert query',11),[])
        hybrid=coll.query.hybrid.call_args.kwargs
        self.assertEqual((hybrid['query'],hybrid['alpha'],hybrid['limit']),('inert query',0.25,7))
        semantic=coll.query.near_text.call_args.kwargs
        self.assertEqual((semantic['query'],semantic['limit']),('inert query',11))

class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_vector_aliases_share_existing_index_top_k_and_answer_style_are_executed(self):
        chunk={'content':'Inert','source_file':'inert.txt','chunk_index':0,'score':1}
        for mode in ('hnsw','flat'):
            with patch.object(rag.ollama,'chat',new=AsyncMock(side_effect=['Reformulated','Answer'])) as chat, \
                 patch.object(rag.ollama,'embed',new=AsyncMock(return_value=[0.1,0.2])), \
                 patch.object(wc,'near_vector_query',new=AsyncMock(return_value=[chunk])) as query:
                response=await rag.run_query('Inert','Synthetic',mode,17,0.25,True,'engineer')
                query.assert_awaited_once_with('Synthetic',[0.1,0.2],17)
                self.assertEqual(chat.await_args_list[-1].args[0],rag.SYNTHESIS_ENGINEER_SYSTEM)
                self.assertEqual(response['chunks_retrieved'],1)

    async def test_hybrid_alpha_top_k_and_semantic_method_forward_to_backend(self):
        with patch.object(rag.ollama,'chat',new=AsyncMock(return_value='Synthetic')), \
             patch.object(wc,'hybrid_query',new=AsyncMock(return_value=[])) as hybrid, \
             patch.object(wc,'near_text_query',new=AsyncMock(return_value=[])) as semantic:
            await rag.run_query('Inert','Synthetic','hybrid',19,0.4,False,'end_user')
            hybrid.assert_awaited_once_with('Synthetic','Synthetic',0.4,19)
            await rag.run_query('Inert','Synthetic','semantic',11,0.4,False,'end_user')
            semantic.assert_awaited_once_with('Synthetic','Synthetic',11)

    async def test_saved_settings_roundtrip_keeps_legacy_ef_but_ui_equivalent_clears_it(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(settings,'upload_dir',directory),patch.object(saved,'_DIR',None):
            config={'collection':'Synthetic','retrieval_mode':'flat','top_k':17,'alpha':0.25,'ef':96,'response_format':'engineer'}
            saved.save(config)
            self.assertEqual(saved.resolve('Synthetic'),(config,False))
            config.update(retrieval_mode='hnsw',ef=None)
            saved.save(config)
            self.assertIsNone(saved.load('Synthetic')['ef'])

class TargetTests(unittest.TestCase):
    def test_in_container_target_cannot_silently_select_another_stack(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('compose_target',Path(__file__).resolve().parents[1]/'verify/compose_target.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        match=module.matches_local_proxy
        for api in ('http://localhost:18080/api','http://127.0.0.1:18080/api/'):
            self.assertTrue(match(api,'127.0.0.1:18080'))
        self.assertTrue(match('http://[::1]:18080/api','[::]:18080'))
        for api in ('http://remote.example:18080/api','http://127.0.0.1:8080/api','https://localhost:18080/api','http://localhost:18080/other','http://user@localhost:18080/api','http://localhost:18080/api?other=1'):
            self.assertFalse(match(api,'127.0.0.1:18080'))
        self.assertFalse(match('http://localhost:18080/api',''))

class DocumentationTests(unittest.TestCase):
    def test_changed_embedded_sources_match_runtime(self):
        root=Path(__file__).resolve().parents[2];text=(root/'IMPLEMENTATION.md').read_text()
        for name in ('api/models/schemas.py','api/routers/collections.py','api/services/weaviate_client.py','ui/src/api/client.ts','ui/src/pages/RetrievalPage.tsx','ui/src/pages/QAPage.tsx','scripts/verify/03_query.sh','scripts/verify/11_retrieval.sh','scripts/verify/retrieval_controls.py','scripts/verify/README.md','scripts/verify/compose_target.py','scripts/verify/browser/ui_criteria.js'):
            lang='typescript' if name.endswith(('.ts','.tsx')) else 'bash' if name.endswith('.sh') else 'markdown' if name.endswith('.md') else 'javascript' if name.endswith('.js') else 'python';fence='````' if name.endswith('.md') else '```';h='### '+name+'\n\n'+fence+lang+'\n'
            a=text.index(h)+len(h);b=text.index('\n'+fence+'\n',a)
            with self.subTest(file=name):self.assertEqual(text[a:b],(root/name).read_text().rstrip('\n'))

if __name__=='__main__':unittest.main()
