"""Import validation regressions; real modules, disposable files, no model/DB calls."""
import copy
import json
import os
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

api_dir = os.environ.get('RAG_TEST_API_DIR')
sys.path.insert(0, api_dir or str(Path(__file__).resolve().parents[2] / 'api'))
from config import settings
from services import goldstandard as gs, importer, packager


def session(sid='gs_0123abcd'):
    return {'session_id': sid, 'collection': 'Corpus', 'status': 'completed',
            'pairs_total': 1, 'pairs_completed': 1, 'pairs': [
                {'pair_id': 'p_0123abcd', 'question': 'Question?', 'answer': 'Answer',
                 'ground_truth': 'Answer', 'contexts': ['Context'],
                 'source_file': 'source.txt', 'chunk_index': 0, 'status': 'approved'}]}


class SessionImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(settings, 'upload_dir', str(self.root / 'uploads')))
        self.stack.enter_context(patch.object(settings, 'sources_dir', str(self.root / 'sources')))
        self.stack.enter_context(patch.object(settings, 'exports_dir', str(self.root / 'exports')))
        self.stack.enter_context(patch.dict(gs._sessions, {}, clear=True))
        self.stack.enter_context(patch.dict(importer._jobs, {}, clear=True))
        self.stack.enter_context(patch.object(importer, '_log'))
        self.pkg = self.root / 'package'
        (self.pkg / 'goldstandard').mkdir(parents=True)
        (self.root / 'uploads').mkdir()
        self.original = session()

    def sidecar(self, name, data):
        (self.pkg / 'goldstandard' / name).write_text(json.dumps(data))

    def archive(self):
        files = {str(p.relative_to(self.pkg)): 'sha256:' + packager.sha256_file(p)
                 for p in self.pkg.rglob('*.json') if p.name != 'manifest.json'}
        manifest = {'package_format': 1, 'collection': {'name': 'Corpus', 'chunk_count': 0},
                    'embedding': {'model': settings.embed_model}, 'files': files}
        (self.pkg / 'manifest.json').write_text(json.dumps(manifest))
        out = self.root / 'exports'
        out.mkdir(exist_ok=True)
        with tarfile.open(out / 'fixture.tar.gz', 'w:gz') as tar:
            tar.add(self.pkg, arcname='package')

    def test_generated_ids_are_accepted_without_normalizing(self):
        for sid in ('gs_0123abcd', 'gs_ffffffff', 'gs_00000000'):
            self.assertEqual(gs._session_path(sid).name, sid + '.json')

    def test_invalid_identity_is_rejected_before_directory_creation(self):
        for sid in ('', 'gs_123', 'GS_0123ABCD', 'gs_0123abcd\n', 123, None):
            with self.subTest(sid=sid), self.assertRaises(ValueError):
                gs._session_path(sid)
        self.assertFalse((self.root / 'uploads' / 'goldstandard_sessions').exists())

    def test_resolved_write_destination_stays_in_session_directory(self):
        outside = self.root / 'existing.json'
        outside.write_text('existing')
        directory = self.root / 'uploads' / 'goldstandard_sessions'
        directory.mkdir()
        (directory / 'gs_0123abcd.json').symlink_to(outside)
        with self.assertRaises(ValueError):
            gs.store_session(self.original)
        self.assertEqual(outside.read_text(), 'existing')
        self.assertNotIn('gs_0123abcd', gs._sessions)

    def test_invalid_store_does_not_change_cache_or_disk(self):
        data = session('not-a-generated-id')
        with self.assertRaises(ValueError):
            gs.store_session(data)
        self.assertFalse(gs._sessions)
        self.assertFalse((self.root / 'uploads' / 'goldstandard_sessions').exists())

    def test_preflight_rejects_malformed_json(self):
        (self.pkg / 'goldstandard' / 'session.json').write_text('{')
        with self.assertRaises(packager.PackageError) as error:
            importer._read_goldstandard_sessions(self.pkg, 'Corpus')
        self.assertEqual(error.exception.code, 'PACKAGE_CORRUPT')
        self.assertEqual(error.exception.detail['file'], 'goldstandard/session.json')

    def test_preflight_rejects_schema_errors_and_wrong_collection(self):
        invalid = [[], {'session_id': 'gs_0123abcd'},
                   {**self.original, 'pairs': 'not a list'},
                   {**self.original, 'pairs_completed': '1'},
                   {**self.original, 'collection': 'Another'},
                   {**self.original, 'pairs': [{}]}]
        for data in invalid:
            with self.subTest(data=data):
                self.sidecar('session.json', data)
                with self.assertRaises(packager.PackageError):
                    importer._read_goldstandard_sessions(self.pkg, 'Corpus')

    def test_preflight_rejects_duplicate_identity(self):
        self.sidecar('first.json', self.original)
        self.sidecar('second.json', self.original)
        with self.assertRaises(packager.PackageError):
            importer._read_goldstandard_sessions(self.pkg, 'Corpus')

    def test_legacy_defaults_and_validity_metadata_survive_valid_import(self):
        data = {**self.original, 'stale': True, 'stale_reason': 'historical',
                'stale_at': '2026-09-27T00:00:00Z', 'orphaned': True,
                'orphaned_reason': 'previous collection removed'}
        self.sidecar('session.json', data)
        before = copy.deepcopy(data)
        validated = importer._read_goldstandard_sessions(self.pkg, 'corpus')
        self.assertEqual(validated, [before])
        notes = importer._restore_sidecars('Imported', self.pkg, 'Corpus', validated)
        saved = json.loads(gs._session_path(data['session_id']).read_text())
        self.assertEqual(saved['collection'], 'Imported')
        self.assertEqual(saved['pairs'], data['pairs'])
        self.assertTrue(saved['stale'])
        self.assertNotIn('orphaned', saved)
        self.assertEqual(validated, [before])
        self.assertIn('1 gold-standard session(s) restored', notes)

    def test_all_sessions_preflight_before_models_build_or_replacement(self):
        self.sidecar('first.json', self.original)
        self.sidecar('second.json', session('not-a-generated-id'))
        manifest = {'collection': {'name': 'Corpus'}, 'embedding': {'model': settings.embed_model}}
        (self.pkg / 'manifest.json').write_text(json.dumps(manifest))
        for conflict in ('abort', 'rename', 'replace'):
            with self.subTest(conflict=conflict), ExitStack() as mocks:
                mocks.enter_context(patch.object(packager, 'open_package', return_value=(self.pkg, manifest)))
                mocks.enter_context(patch.object(packager, 'verify_digests'))
                ensure = mocks.enter_context(patch.object(importer, '_ensure_models'))
                build = mocks.enter_context(patch.object(importer, '_build'))
                exists = mocks.enter_context(patch.object(importer.wc, '_collection_exists_sync'))
                delete = mocks.enter_context(patch.object(importer.wc, '_delete_collection_sync'))
                client = mocks.enter_context(patch.object(importer.wc, 'get_client',
                                                          side_effect=AssertionError('Backend call during preflight')))
                restore = mocks.enter_context(patch.object(importer, '_restore_sidecars'))
                importer._jobs['test'] = {'status': 'queued'}
                importer._run('test', 'fixture.tar.gz', conflict)
                self.assertEqual(importer._jobs['test']['status'], 'failed')
                self.assertEqual(importer._jobs['test']['error_code'], 'PACKAGE_CORRUPT')
                for operation in (ensure, build, exists, delete, client, restore):
                    operation.assert_not_called()
                self.assertFalse(gs._sessions)
                self.assertFalse((self.root / 'uploads' / 'goldstandard_sessions').exists())
                self.assertFalse((self.root / 'sources').exists())

    def test_digest_valid_archive_failure_preserves_existing_review(self):
        gs.store_session(self.original)
        saved = gs._session_path(self.original['session_id'])
        original_bytes = saved.read_bytes()
        self.sidecar('first.json', self.original)
        self.sidecar('second.json', session('not-a-generated-id'))
        self.archive()
        for conflict in ('abort', 'rename', 'replace'):
            with self.subTest(conflict=conflict), ExitStack() as mocks:
                ensure = mocks.enter_context(patch.object(importer, '_ensure_models'))
                client = mocks.enter_context(patch.object(importer.wc, 'get_client',
                                                          side_effect=AssertionError('Backend call during preflight')))
                exists = mocks.enter_context(patch.object(importer.wc, '_collection_exists_sync'))
                importer._jobs['test'] = {'status': 'queued'}
                importer._run('test', 'fixture.tar.gz', conflict)
                self.assertEqual(importer._jobs['test']['error_code'], 'PACKAGE_CORRUPT')
                self.assertEqual(saved.read_bytes(), original_bytes)
                self.assertEqual(gs._sessions[self.original['session_id']], self.original)
                for operation in (ensure, client, exists):
                    operation.assert_not_called()
                self.assertFalse(list((self.root / 'uploads').glob('import-*')))

    def test_valid_archive_import_restores_evaluation(self):
        self.sidecar('session.json', self.original)
        self.archive()
        with patch.object(importer, '_ensure_models', return_value=[]), \
             patch.object(importer.wc, '_collection_exists_sync', return_value=False), \
             patch.object(importer, '_build', return_value=0):
            importer._jobs['test'] = {'status': 'queued'}
            importer._run('test', 'fixture.tar.gz', 'abort')
        self.assertEqual(importer._jobs['test']['status'], 'completed')
        self.assertEqual(json.loads(gs._session_path(self.original['session_id']).read_text()),
                         self.original)


if __name__ == '__main__':
    unittest.main()
