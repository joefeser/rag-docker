"""Direct/saved settings reject invalid inputs before model/backend work."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR', str(Path(__file__).resolve().parents[2] / 'api')))
from fastapi.testclient import TestClient
from pydantic import ValidationError
from config import settings
from main import app
from models.schemas import CreateCollectionRequest, HnswConfig, IngestConfig, QueryRequest, RechunkRequest, ReembedRequest, SaveRetrievalConfigBody
from routers import collections, ingest, tuning as tuning_router, query, retrieval_config as retrieval_router
from services import weaviate_client as wc, chunker, rag_pipeline, ingest_config, retrieval_config, ingest_pipeline


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(settings, 'upload_dir', self.temp))
        self.stack.enter_context(patch.object(ingest_config, '_DIR', None))
        self.stack.enter_context(patch.object(retrieval_config, '_DIR', None))
        self.exists = self.stack.enter_context(patch.object(wc, 'collection_exists', new=AsyncMock(return_value=True)))
        self.create = self.stack.enter_context(patch.object(wc, 'create_collection', new=AsyncMock()))
        self.ingest_job = self.stack.enter_context(patch.object(ingest.ingest_pipeline, 'start_ingest_job', new=AsyncMock(return_value='ingest')))
        self.tune_job = self.stack.enter_context(patch.object(tuning_router.tuning, 'start_tune_job', new=AsyncMock(return_value='tune')))
        self.query_work = self.stack.enter_context(patch.object(query.rag_pipeline, 'run_query', new=AsyncMock()))
        self.client = TestClient(app)  # No lifespan: these tests never connect a stack.
        self.addCleanup(self.client.close)

    def assert_rejected_without_work(self, path, payload):
        self.exists.reset_mock(); self.create.reset_mock(); self.ingest_job.reset_mock(); self.tune_job.reset_mock(); self.query_work.reset_mock()
        response = self.client.post(path, json=payload)
        self.assertEqual(response.status_code, 422, response.text)
        for work in (self.exists, self.create, self.ingest_job, self.tune_job, self.query_work): work.assert_not_called()
        self.assertEqual(list(Path(self.temp).rglob('*.json')), [])

    def test_invalid_collection_enums_and_hnsw_ranges_precede_backend(self):
        for update in ({'index_type': 'unknown'}, {'distance_metric': 'unknown'},
                       {'hnsw_config': {'efConstruction': 63}}, {'hnsw_config': {'efConstruction': 513}},
                       {'hnsw_config': {'maxConnections': 15}}, {'hnsw_config': {'maxConnections': 129}},
                       {'hnsw_config': {'ef': 15}}, {'hnsw_config': {'ef': 513}}):
            with self.subTest(update=update): self.assert_rejected_without_work('/collections', {'name': 'ReviewSettings', **update})

    def test_invalid_query_and_saved_retrieval_share_contract(self):
        for update in ({'retrieval_mode': 'unknown'}, {'response_format': 'unknown'}, {'top_k': 0},
                       {'top_k': 51}, {'alpha': -0.1}, {'alpha': 1.1}, {'top_k': True}, {'alpha': False}):
            for route in ('/query', '/retrieval/config'):
                with self.subTest(route=route, update=update):
                    self.assert_rejected_without_work(route, {'collection': 'ReviewSettings', 'question': 'synthetic', **update})

    def test_invalid_reindex_enums_precede_backend_and_job(self):
        for update in ({'index_type': 'unknown'}, {'distance_metric': 'unknown'}):
            self.assert_rejected_without_work('/tune/reindex', {'collection': 'ReviewSettings', **update})

    def test_saved_retrieval_ef_numeric_bounds(self):
        for ef in (0, 15, 513, True):
            self.assert_rejected_without_work('/retrieval/config', {'collection': 'ReviewSettings', 'ef': ef})

    def test_chunking_errors_on_saved_and_tuning_surfaces_precede_work(self):
        for update in ({'chunking_strategy': 'unknown'}, {'chunk_size': 0}, {'chunk_size': -1},
                       {'chunk_overlap': -1}, {'chunk_overlap': 1000}, {'min_chunk_size': -1},
                       {'min_chunk_size': 1001}, {'similarity_threshold': -0.1}, {'similarity_threshold': 1.1},
                       {'chunk_size': True}, {'similarity_threshold': False}):
            for route in ('/ingest/config', '/tune/rechunk', '/tune/reembed'):
                with self.subTest(route=route, update=update): self.assert_rejected_without_work(route, {'collection': 'ReviewSettings', **update})

    def test_multipart_invalid_chunking_precedes_collection_lookup_and_file_staging(self):
        for update in ({'strategy': 'unknown'}, {'chunk_size': '0'}, {'chunk_overlap': '-1'},
                       {'chunk_overlap': '1000'}, {'similarity_threshold': 'nan'}, {'min_chunk_size': '-1'}):
            self.exists.reset_mock(); self.ingest_job.reset_mock()
            response = self.client.post('/ingest/upload', data={'collection': 'ReviewSettings', **update}, files={'files': ('source.txt', b'inert synthetic text')})
            self.assertEqual(response.status_code, 422, response.text)
            self.exists.assert_not_called(); self.ingest_job.assert_not_called()
            self.assertEqual(list(Path(self.temp).iterdir()), [])

    def test_nonfinite_json_is_a_serializable_422(self):
        for token in ('NaN', 'Infinity', '-Infinity'):
            for route, field in (('/query', 'alpha'), ('/retrieval/config', 'alpha'), ('/ingest/config', 'similarity_threshold'), ('/tune/rechunk', 'similarity_threshold')):
                self.exists.reset_mock(); self.query_work.reset_mock()
                body = '{"question":"synthetic","collection":"ReviewSettings","' + field + '":' + token + '}'
                response = self.client.post(route, content=body, headers={'Content-Type': 'application/json'})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertTrue(response.json().get('detail'))
                self.exists.assert_not_called(); self.query_work.assert_not_called()

    def test_valid_defaults_and_boundary_settings(self):
        self.assertEqual(CreateCollectionRequest(name='ReviewSettings').hnsw_config.model_dump(), {'efConstruction': 128, 'maxConnections': 64, 'ef': 64})
        for values in ((64, 16, 16), (512, 128, 512)):
            HnswConfig(efConstruction=values[0], maxConnections=values[1], ef=values[2])
        for mode in ('hnsw', 'flat', 'hybrid', 'semantic'):
            QueryRequest(question='synthetic', collection='ReviewSettings', retrieval_mode=mode, top_k=1, alpha=0)
            QueryRequest(question='synthetic', collection='ReviewSettings', retrieval_mode=mode, top_k=50, alpha=1)
        self.assertEqual(RechunkRequest(collection='ReviewSettings', min_chunk_size=0).chunking()['min_chunk_size'], 0)
        self.assertFalse(ReembedRequest(collection='ReviewSettings').has_chunking())
        self.assertFalse(ReembedRequest(collection='ReviewSettings', chunk_size=None).has_chunking())
        self.assertEqual(RechunkRequest(collection='ReviewSettings').chunking()['chunk_size'], 1000)

    def test_applicable_overlap_and_minimum_relationships_preserve_ignored_defaults(self):
        IngestConfig(chunking_strategy='fixed', chunk_size=150, min_chunk_size=40)  # default overlap200 is unused
        IngestConfig(chunking_strategy='context_aware', chunk_size=150, min_chunk_size=40, chunk_overlap=1000)
        IngestConfig(chunking_strategy='semantic', min_chunk_size=2000)
        for strategy in ('overlap', 'language'):
            with self.assertRaises(ValidationError): IngestConfig(chunking_strategy=strategy, chunk_overlap=1000)
        IngestConfig(chunking_strategy='overlap', chunk_size=201, chunk_overlap=200, min_chunk_size=0)

    def test_saved_valid_ingest_and_retrieval_round_trips(self):
        ingest_body = {'collection': 'ReviewSettings', 'chunking_strategy': 'fixed', 'chunk_size': 150, 'min_chunk_size': 40}
        response = self.client.post('/ingest/config', json=ingest_body)
        self.assertEqual(response.status_code, 201, response.text)
        saved = self.client.get('/ingest/config/ReviewSettings').json()
        self.assertEqual(saved['chunk_size'], 150); self.assertEqual(saved['chunk_overlap'], 200); self.assertFalse(saved['is_default'])
        response = self.client.post('/retrieval/config', json={'collection': 'ReviewSettings', 'retrieval_mode': 'hybrid', 'top_k': 50, 'alpha': 1, 'ef': 512, 'response_format': 'engineer'})
        self.assertEqual(response.status_code, 201, response.text)
        saved = self.client.get('/retrieval/config/ReviewSettings').json()
        self.assertEqual(saved['top_k'], 50); self.assertEqual(saved['ef'], 512); self.assertFalse(saved['is_default'])

    def test_valid_multipart_defaults_reach_existing_job_route(self):
        response = self.client.post('/ingest/upload', data={'collection': 'ReviewSettings'}, files={'files': ('source.txt', b'inert synthetic text')})
        self.assertEqual(response.status_code, 202, response.text)
        self.ingest_job.assert_awaited_once()
        self.assertEqual(self.ingest_job.call_args.kwargs['chunk_overlap'], 200)

    def test_unconfigured_reembed_preserves_no_rechunk_behavior(self):
        response = self.client.post('/tune/reembed', json={'collection': 'ReviewSettings'})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.tune_job.call_args.args[2], {'chunking': None})


class InternalBoundaryTests(unittest.TestCase):
    def test_collection_creation_rejects_bad_settings_before_client_access(self):
        for index, distance, config in (('unknown','cosine',{}), ('hnsw','unknown',{}), ('hnsw','cosine',{'ef':0})):
            with patch.object(wc, 'get_client') as client:
                with self.assertRaises(ValidationError): wc._create_collection_sync('ReviewSettings', index, distance, config)
                client.assert_not_called()

    def test_chunker_rejects_before_semantic_model_or_splitter_work(self):
        for strategy, size, overlap in (('unknown',1000,200), ('semantic',0,200), ('overlap',1000,1000)):
            with patch.object(chunker, '_get_semantic_model') as model:
                with self.assertRaises(ValidationError): chunker.chunk('inert text',strategy,chunk_size=size,chunk_overlap_size=overlap)
                model.assert_not_called()

    def test_context_fallback_ignores_overlap_as_documented(self):
        with patch.object(chunker, 'chunk_language', return_value=['inert']) as split:
            chunker.chunk('inert', 'context_aware', chunk_size=150, chunk_overlap_size=1000, min_chunk_size=40)
        self.assertEqual(split.call_args.args[2], 0)

    def test_direct_ingest_rejects_before_upload_staging_or_job_creation(self):
        with patch.object(ingest_pipeline.tempfile, 'mkdtemp') as stage, patch.dict(ingest_pipeline._jobs, {}, clear=True):
            with self.assertRaises(ValidationError):
                asyncio.run(ingest_pipeline.start_ingest_job([], 'ReviewSettings', 'unknown', 1000, 200, 0.85, 100))
            stage.assert_not_called()
            self.assertEqual(ingest_pipeline._jobs, {})

    def test_query_rejects_before_llm_or_retrieval(self):
        # The HTTP test patch is absent here: exercise the real service entry.
        with patch.object(rag_pipeline.ollama, 'chat', new=AsyncMock()) as chat:
            with self.assertRaises(ValidationError): asyncio.run(rag_pipeline.run_query('synthetic','ReviewSettings','unknown',5,0.5,False,'end_user'))
            chat.assert_not_called()


class ImplementationTests(unittest.TestCase):
    def test_embedded_changed_sources_match_runtime(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / 'IMPLEMENTATION.md').read_text()
        names = ['api/models/schemas.py', 'api/main.py', 'api/routers/ingest.py',
                 'api/services/chunker.py', 'api/services/ingest_pipeline.py',
                 'api/services/rag_pipeline.py', 'api/services/weaviate_client.py',
                 'scripts/verify/settings_validation.py']
        for name in names:
            with self.subTest(file=name):
                header = '### ' + name + '\n\n```python\n'
                start = text.index(header) + len(header)
                end = text.index('\n```\n', start)
                self.assertEqual(text[start:end], (root / name).read_text().rstrip('\n'))


if __name__ == '__main__': unittest.main()
