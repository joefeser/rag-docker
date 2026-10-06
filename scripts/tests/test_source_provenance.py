"""Source identity refuses unsafe re-chunking while preserving legacy reindex."""
import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from config import settings
from services import ingest_pipeline as ingest, sources, tuning, weaviate_client as wc
from routers.tuning import tune_options


class ProvenanceTests(unittest.TestCase):
    def test_failed_retention_still_records_the_inserted_bytes_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / 'upload'
            stage.mkdir()
            path = stage / 'a.txt'
            path.write_bytes(b'new version')
            job = dict(files_total=1, files_completed=0, files_failed=0, chunks_stored=0, errors=[])
            with patch.dict(ingest._jobs, {'owned': job}), \
                 patch.object(ingest, '_parse_file', return_value=('new version', [])), \
                 patch.object(ingest, 'do_chunk', return_value=['new version']), \
                 patch.object(wc, '_insert_chunks_sync') as insert, \
                 patch.object(sources, 'store', side_effect=OSError('retention failed')):
                ingest._process_job_sync('owned', [path], stage, 'Corpus', 'fixed', 1000, 0, .85, 100)
            self.assertEqual(job['status'], 'completed')
            self.assertEqual(insert.call_args.args[1][0]['source_digest'], hashlib.sha256(b'new version').hexdigest())

    def test_bounded_details_and_get_options_share_refusal(self):
        rows = [SimpleNamespace(properties={'source_file': f'{i:04d}' + 'x' * 500}) for i in range(101)]
        collection = SimpleNamespace(iterator=lambda: iter(rows))
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: collection))
        with patch.object(wc, 'get_client', return_value=client), \
             patch.object(sources, 'load_index', return_value={'documents': {}}), \
             patch.object(sources, 'has_sources', return_value=True), \
             patch.object(sources, 'stats', return_value={'document_count': 1}), \
             patch.object(wc, 'collection_exists', new=AsyncMock(return_value=True)):
            with self.assertRaises(tuning.PackageError) as caught:
                tuning.require_source_coverage('Corpus')
            detail = caught.exception.detail
            self.assertEqual(len(detail['uncovered_source_files']), 100)
            self.assertTrue(all(len(name) <= 256 for name in detail['uncovered_source_files']))
            self.assertTrue(detail['uncovered_source_files_truncated'])
            options = asyncio.run(tune_options('Corpus'))
            self.assertFalse(options.can_rechunk)
            self.assertTrue(options.can_reembed and options.can_reindex)

    def test_legacy_creation_preserves_schema_without_digest(self):
        client = MagicMock()
        with patch.object(wc, 'get_client', return_value=client):
            wc._create_collection_sync('Legacy', 'hnsw', 'cosine', {}, source_digest=False)
        properties = client.collections.create.call_args.kwargs['properties']
        self.assertNotIn('source_digest', [p.name for p in properties])
        self.assertEqual(len(properties), 8)

    def test_digest_property_never_changes_embedding_input(self):
        self.assertTrue(wc.SOURCE_DIGEST_PROPERTY._to_dict()['skip_vectorization'])


if __name__ == '__main__':
    unittest.main()
