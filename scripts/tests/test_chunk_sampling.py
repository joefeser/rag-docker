"""Synthetic sampling invariants and validation before persistence/model work."""
import asyncio
import os
import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR', str(Path(__file__).resolve().parents[2]/'api')))
from fastapi.testclient import TestClient
from main import app
from models.schemas import GenerateRequest
from services import chunk_sampling as sample, goldstandard as gs, weaviate_client as wc


def objects(count=20):
    return [SimpleNamespace(uuid=UUID(int=i), properties={
        'content':f'synthetic {i}', 'source_file':'inert.txt', 'chunk_index':i,
    }) for i in range(1,count+1)]


def ids(rows):
    return [UUID(row['object_id']).int for row in rows]


class SelectionTests(unittest.TestCase):
    def test_known_seed_selects_from_entire_population_in_stable_order(self):
        self.assertEqual(ids(sample.select_chunks(objects(),3,7)),[7,10,12])

    def test_repeat_reverse_and_shuffled_backend_order_are_equivalent(self):
        candidates=objects(100)
        expected=sample.select_chunks(candidates,20,-9)
        shuffled=candidates.copy(); random.Random(5).shuffle(shuffled)
        for order in (candidates,list(reversed(candidates)),shuffled):
            self.assertEqual(sample.select_chunks(iter(order),20,-9),expected)

    def test_empty_small_and_oversize_requests_report_available_unique_objects(self):
        self.assertEqual(sample.select_chunks([],20,1),[])
        self.assertEqual(set(ids(sample.select_chunks(objects(3),100,1))),{1,2,3})
        repeated=objects(5)*3
        self.assertEqual(len(sample.select_chunks(repeated,100,1)),5)
        self.assertEqual(len(sample.select_chunks(repeated,3,1)),3)

    def test_different_seeds_need_not_select_different_subsets(self):
        self.assertEqual(set(ids(sample.select_chunks(objects(3),3,1))),
                         set(ids(sample.select_chunks(objects(3),3,2))))
        self.assertNotEqual(ids(sample.select_chunks(objects(20),3,1)),
                            ids(sample.select_chunks(objects(20),3,2)))

    def test_null_seed_draws_one_nonce_seeded_call_uses_no_entropy(self):
        with patch.object(sample.secrets,'token_bytes',return_value=b'a'*32) as entropy:
            self.assertEqual(ids(sample.select_chunks(objects(),3,None)),[17,4,9])
            entropy.assert_called_once_with(32)
        with patch.object(sample.secrets,'token_bytes',side_effect=AssertionError('unexpected entropy')):
            sample.select_chunks(objects(),3,0)

    def test_calls_do_not_change_global_pseudorandom_state(self):
        before=random.getstate()
        sample.select_chunks(objects(),3,7)
        sample.select_chunks(objects(),3,None)
        self.assertEqual(random.getstate(),before)

    def test_hash_collision_uses_uuid_tie_breaker_without_comparing_payloads(self):
        class Collision:
            def copy(self): return self
            def update(self,value): pass
            def digest(self): return b'\0'*32
        with patch.object(sample.hashlib,'sha256',return_value=Collision()):
            self.assertEqual(ids(sample.select_chunks(reversed(objects()),3,7)),[1,2,3])

    def test_scans_full_population_with_at_most_requested_candidates(self):
        heap_sizes=[]; visited=[]
        real_push=sample.heapq.heappush; real_replace=sample.heapq.heapreplace
        def push(heap,item):
            real_push(heap,item); heap_sizes.append(len(heap))
        def replace(heap,item):
            result=real_replace(heap,item); heap_sizes.append(len(heap)); return result
        def stream():
            for candidate in objects(10000):
                visited.append(candidate.uuid); yield candidate
        with patch.object(sample.heapq,'heappush',side_effect=push), patch.object(sample.heapq,'heapreplace',side_effect=replace):
            rows=sample.select_chunks(stream(),5,7)
        self.assertEqual(len(visited),10000)
        self.assertEqual(len(rows),5)
        self.assertLessEqual(max(heap_sizes),5)
        self.assertTrue(any(UUID(row['object_id']).int>100 for row in rows))

    def test_invalid_limits_seeds_and_object_identities_fail(self):
        for limit in (0,-1,101,True,1.5,'3',None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                sample.select_chunks(objects(),limit,7)
        for seed in (True,1.5,'3'):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                sample.select_chunks(objects(),3,seed)
        with self.assertRaises(ValueError):
            sample.select_chunks([SimpleNamespace(uuid='invalid',properties={})],3,7)


class AdapterTests(unittest.TestCase):
    def test_sdk_iterator_fetches_only_required_properties_without_vectors(self):
        collection=MagicMock(); collection.iterator.return_value=iter(objects())
        client=MagicMock(); client.collections.get.return_value=collection
        with patch.object(wc,'get_client',return_value=client):
            self.assertEqual(ids(wc._sample_chunks_sync('Inert',3,7)),[7,10,12])
        collection.iterator.assert_called_once_with(include_vector=False,
            return_properties=['content','source_file','chunk_index'],cache_size=100)
        collection.query.fetch_objects.assert_not_called()

    def test_invalid_settings_are_rejected_before_backend_client(self):
        with patch.object(wc,'get_client',side_effect=AssertionError('backend contacted')):
            for limit,seed in ((0,1),(101,1),(True,1),(3,False),(3,float('nan'))):
                with self.subTest(settings=(limit,seed)), self.assertRaises(ValueError):
                    wc._sample_chunks_sync('Inert',limit,seed)


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_sessions=gs._sessions
        gs._sessions={}

    async def asyncTearDown(self):
        gs._sessions=self.original_sessions

    async def test_seed_reaches_sampler_actual_size_drives_generation(self):
        chosen=sample.select_chunks(objects(3),20,7)
        with patch.object(gs.wc,'sample_chunks',new=AsyncMock(return_value=chosen)) as sampler, \
             patch.object(gs,'_save_session',new=AsyncMock()) as save, \
             patch.object(gs,'_run_generation',new=AsyncMock()) as generate:
            result=await gs.start_generation('Inert',20,7)
            await asyncio.sleep(0)
            sampler.assert_awaited_once_with('Inert',limit=20,seed=7)
            self.assertEqual(result['pairs_total'],3)
            self.assertEqual(gs._sessions[result['session_id']]['pairs_total'],3)
            save.assert_awaited_once()
            generate.assert_awaited_once_with(result['session_id'],chosen)

    async def test_invalid_settings_and_selection_failure_prevent_session_and_model_work(self):
        with patch.object(gs.wc,'sample_chunks',new=AsyncMock(side_effect=ValueError('bad identity'))) as sampler, \
             patch.object(gs,'_save_session',new=AsyncMock()) as save, \
             patch.object(gs,'_run_generation',new=AsyncMock()) as generate:
            for limit,seed in ((0,7),(101,7),(3,True),(3,float('inf'))):
                with self.subTest(settings=(limit,seed)), self.assertRaises(ValueError):
                    await gs.start_generation('Inert',limit,seed)
            sampler.assert_not_awaited()
            with self.assertRaises(ValueError):
                await gs.start_generation('Inert',3,7)
            self.assertEqual(gs._sessions,{})
            save.assert_not_awaited(); generate.assert_not_called()


class RequestTests(unittest.TestCase):
    def test_valid_defaults_bounds_and_existing_numeric_coercion(self):
        self.assertEqual(GenerateRequest(collection='Inert').sample_size,20)
        self.assertIsNone(GenerateRequest(collection='Inert').seed)
        for value in (1,100,'3',3.0):
            self.assertEqual(GenerateRequest(collection='Inert',sample_size=value,seed='-7').seed,-7)

    def test_invalid_requests_return_422_before_collection_lookup_or_generation(self):
        client=TestClient(app)  # No context manager: do not run startup sweeps.
        with patch.object(wc,'collection_exists',new=AsyncMock(side_effect=AssertionError('backend contacted'))) as lookup, \
             patch.object(gs,'start_generation',new=AsyncMock()) as generation:
            for update in ({'sample_size':0},{'sample_size':101},{'sample_size':True},
                           {'sample_size':1.5},{'seed':False},{'seed':1.5}):
                response=client.post('/goldstandard/generate',json={'collection':'Inert',**update})
                self.assertEqual(response.status_code,422,response.text)
            for field in ('sample_size','seed'):
                for number in ('NaN','Infinity','-Infinity'):
                    response=client.post('/goldstandard/generate',content=
                        '{"collection":"Inert","'+field+'":'+number+'}',
                        headers={'Content-Type':'application/json'})
                    self.assertEqual(response.status_code,422,response.text)
                    self.assertIn('detail',response.json())
            lookup.assert_not_awaited(); generation.assert_not_awaited()


class ImplementationTests(unittest.TestCase):
    def test_changed_embedded_sources_match_runtime(self):
        root=Path(__file__).resolve().parents[2]
        text=(root/'IMPLEMENTATION.md').read_text()
        names=('api/main.py','api/models/schemas.py','api/services/weaviate_client.py',
               'api/services/goldstandard.py','api/services/chunk_sampling.py',
               'scripts/verify/chunk_sampling.py','scripts/verify/README.md')
        for name in names:
            fence='````' if name.endswith('README.md') else '```'
            language='markdown' if name.endswith('README.md') else 'python'
            header='### '+name+'\n\n'+fence+language+'\n'
            start=text.index(header)+len(header); end=text.index('\n'+fence+'\n',start)
            with self.subTest(file=name):
                self.assertEqual(text[start:end],(root/name).read_text().rstrip('\n'))


if __name__=='__main__': unittest.main()
