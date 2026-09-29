"""Bundled-model byte integrity and publication boundary, using inert bytes."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR', str(Path(__file__).resolve().parents[2] / 'api')))
from config import settings
from services import model_bundle as models


class ModelBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pkg = self.root / 'package'
        self.store = self.root / 'store'
        self.model = 'review-model:latest'
        self.patch = patch.object(settings, 'ollama_models_dir', str(self.store))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.src = self.pkg / 'models' / 'review-model'
        (self.src / 'blobs').mkdir(parents=True)
        self.contents = [b'ordinary config bytes', b'ordinary layer bytes']
        self.digests = ['sha256:' + hashlib.sha256(data).hexdigest() for data in self.contents]
        self.manifest = {'config': {'digest': self.digests[0]}, 'layers': [{'digest': self.digests[1]}]}
        self.manifest_file = self.src / 'manifest.json'
        self.manifest_bytes = json.dumps(self.manifest).encode()
        self.manifest_file.write_bytes(self.manifest_bytes)
        for digest, data in zip(self.digests, self.contents):
            (self.src / 'blobs' / digest.replace(':', '-')).write_bytes(data)

    def assert_unpublished(self):
        self.assertFalse(models.manifest_path(self.model).exists())
        self.assertFalse(list(self.store.rglob('*.partial')))

    def test_valid_install_and_existing_content_are_byte_identical_and_unchanged(self):
        models.install_model(self.pkg, self.model)
        self.assertTrue(models.is_installed(self.model))
        self.assertEqual(models.manifest_path(self.model).read_bytes(), self.manifest_bytes)
        stamps = {models.blob_path(d): models.blob_path(d).stat().st_mtime_ns for d in self.digests}
        models.install_model(self.pkg, self.model)
        for digest, content in zip(self.digests, self.contents):
            path = models.blob_path(digest)
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, stamps[path])

    def test_corrupt_package_is_refused_even_when_shared_target_blob_is_healthy(self):
        target = models.blob_path(self.digests[1]); target.parent.mkdir(parents=True)
        target.write_bytes(self.contents[1]); stamp = target.stat().st_mtime_ns
        (self.src / 'blobs' / target.name).write_bytes(b'different inert bytes')
        with self.assertRaisesRegex(ValueError, 'disagree'):
            models.install_model(self.pkg, self.model)
        self.assert_unpublished()
        self.assertEqual(target.read_bytes(), self.contents[1])
        self.assertEqual(target.stat().st_mtime_ns, stamp)
        self.assertFalse(models.blob_path(self.digests[0]).exists())

    def test_corrupt_existing_blob_is_not_overwritten_or_activated(self):
        target = models.blob_path(self.digests[1]); target.parent.mkdir(parents=True)
        target.write_bytes(b'corrupt existing inert bytes')
        with self.assertRaisesRegex(ValueError, 'restore'):
            models.install_model(self.pkg, self.model)
        self.assert_unpublished()
        self.assertEqual(target.read_bytes(), b'corrupt existing inert bytes')

    def test_is_installed_requires_actual_bytes_not_filenames(self):
        models.install_model(self.pkg, self.model)
        models.blob_path(self.digests[1]).write_bytes(b'different bytes')
        self.assertFalse(models.is_installed(self.model))

    def test_invalid_reference_grammar_and_missing_references_are_refused(self):
        for invalid in ('sha256:abc', 'sha512:' + 'a'*64, 'sha256:' + 'A'*64, None, 42):
            with self.subTest(reference=invalid):
                bad = {'config': {'digest': self.digests[0]}, 'layers': [{'digest': invalid}]}
                self.manifest_file.write_text(json.dumps(bad))
                with self.assertRaises(ValueError): models.install_model(self.pkg, self.model)
                self.assert_unpublished()
        for invalid in ({}, {'config': [], 'layers': []}, {'config': {'digest': self.digests[0]}, 'layers': [None]}):
            self.manifest_file.write_text(json.dumps(invalid))
            with self.assertRaises(ValueError): models.install_model(self.pkg, self.model)
            self.assert_unpublished()

    def test_missing_package_blob_does_not_publish_anything(self):
        (self.src / 'blobs' / self.digests[1].replace(':', '-')).unlink()
        with self.assertRaises(FileNotFoundError): models.install_model(self.pkg, self.model)
        self.assert_unpublished()
        self.assertFalse(models.blob_path(self.digests[0]).exists())

    def test_store_and_package_symlinks_are_refused(self):
        outside = self.root / 'outside'; outside.mkdir()
        for source_side in (True, False):
            with self.subTest(package=source_side):
                if source_side:
                    location = self.src / 'blobs' / self.digests[1].replace(':', '-')
                    location.unlink(); destination = outside / 'layer'; destination.write_bytes(self.contents[1])
                else:
                    location = self.store / 'models' / 'blobs'; location.parent.mkdir(parents=True)
                    destination = outside
                location.symlink_to(destination)
                with self.assertRaises(ValueError): models.install_model(self.pkg, self.model)
                self.assertFalse(list(outside.glob('sha256-*')))
                location.unlink()
                if source_side: location.write_bytes(self.contents[1])
        self.assert_unpublished()

    def test_unsafe_model_components_are_refused(self):
        for name in ('review/other', 'review:../tag', '../review', 'review:tag:other'):
            with self.subTest(model=name), self.assertRaises(ValueError): models.install_model(self.pkg, name)
        self.assert_unpublished()

    def test_changed_source_during_copy_cannot_publish_manifest_or_partial_blob(self):
        actual = models._publish
        def change_then_write(path, write, **kwargs):
            if path.name == self.digests[1].replace(':', '-'):
                (self.src / 'blobs' / path.name).write_bytes(b'changed inert bytes')
            return actual(path, write, **kwargs)
        with patch.object(models, '_publish', side_effect=change_then_write):
            with self.assertRaisesRegex(ValueError, 'changed'): models.install_model(self.pkg, self.model)
        self.assert_unpublished()
        self.assertFalse(models.blob_path(self.digests[1]).exists())

    def test_concurrent_valid_blob_publication_is_reused_without_replacement(self):
        actual = models._publish
        def winning_writer(path, write, **kwargs):
            if path.name == self.digests[1].replace(':', '-'):
                path.write_bytes(self.contents[1]); stamp = path.stat().st_mtime_ns
                result = actual(path, write, **kwargs)
                self.assertEqual(path.stat().st_mtime_ns, stamp)
                return result
            return actual(path, write, **kwargs)
        with patch.object(models, '_publish', side_effect=winning_writer): models.install_model(self.pkg, self.model)
        self.assertTrue(models.is_installed(self.model))

    def test_concurrent_corrupt_blob_publication_is_refused(self):
        actual = models._publish
        def winning_writer(path, write, **kwargs):
            if path.name == self.digests[1].replace(':', '-'): path.write_bytes(b'wrong bytes')
            return actual(path, write, **kwargs)
        with patch.object(models, '_publish', side_effect=winning_writer):
            with self.assertRaises(ValueError): models.install_model(self.pkg, self.model)
        self.assert_unpublished()

    def test_captured_manifest_is_published_even_if_package_manifest_changes(self):
        actual = models._publish
        def changing_manifest(path, write, **kwargs):
            self.manifest_file.write_text('{}')
            return actual(path, write, **kwargs)
        with patch.object(models, '_publish', side_effect=changing_manifest): models.install_model(self.pkg, self.model)
        self.assertEqual(models.manifest_path(self.model).read_bytes(), self.manifest_bytes)
        self.assertTrue(models.is_installed(self.model))

    def test_large_blob_is_read_in_bounded_blocks(self):
        data = b'plain inert bytes' * 150000
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        self.manifest['layers'] = [{'digest': digest}]
        self.manifest_file.write_text(json.dumps(self.manifest))
        (self.src / 'blobs' / digest.replace(':', '-')).write_bytes(data)
        actual = Path.open
        reads = []
        test = self
        class Reader:
            def __init__(self, file): self.file = file
            def __enter__(self): self.file.__enter__(); return self
            def __exit__(self, *args): return self.file.__exit__(*args)
            def read(self, size=-1):
                test.assertGreater(size, 0)
                test.assertLessEqual(size, 1024 * 1024)
                reads.append(size)
                return self.file.read(size)
        def bounded(path, *args, **kwargs):
            file = actual(path, *args, **kwargs)
            return Reader(file) if path.name.startswith('sha256-') else file
        with patch.object(Path, 'open', bounded): models.install_model(self.pkg, self.model)
        self.assertGreater(len(reads), 6)
        self.assertEqual(models.blob_path(digest).stat().st_size, len(data))

    def test_manifest_publication_failure_preserves_previous_manifest(self):
        destination = models.manifest_path(self.model); destination.parent.mkdir(parents=True)
        prior = json.dumps({**self.manifest, 'schemaVersion': 2}).encode()
        destination.write_bytes(prior)
        actual = Path.replace
        def fail_manifest(path, target):
            if target == destination: raise OSError('publication unavailable')
            return actual(path, target)
        with patch.object(Path, 'replace', fail_manifest):
            with self.assertRaises(OSError): models.install_model(self.pkg, self.model)
        self.assertEqual(destination.read_bytes(), prior)
        self.assertFalse(list(self.store.rglob('*.partial')))

    def test_import_already_present_model_leaves_manifest_and_shared_blobs_untouched(self):
        from services import importer
        models.install_model(self.pkg, self.model)
        paths = [models.manifest_path(self.model), *(models.blob_path(d) for d in self.digests)]
        stamps = {path: path.stat().st_mtime_ns for path in paths}
        with patch.object(settings, 'embed_model', self.model), patch.object(settings, 'llm_model', self.model):
            notes = importer._ensure_models(self.pkg, {'embedding': {'model': self.model}})
        self.assertTrue(all('already present' in note for note in notes))
        self.assertEqual({path: path.stat().st_mtime_ns for path in paths}, stamps)


class ImplementationTests(unittest.TestCase):
    def test_embedded_model_source_matches_runtime(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / 'IMPLEMENTATION.md').read_text()
        for name, fence, language in [('api/services/model_bundle.py', '```', 'python'),
                                      ('scripts/verify/model_integrity.py', '```', 'python'),
                                      ('scripts/verify/README.md', '````', 'markdown')]:
            with self.subTest(file=name):
                header = '### ' + name + '\n\n' + fence + language + '\n'
                start = text.index(header) + len(header)
                end = text.index('\n' + fence + '\n', start)
                self.assertEqual(text[start:end], (root / name).read_text().rstrip('\n'))


if __name__ == '__main__': unittest.main()
