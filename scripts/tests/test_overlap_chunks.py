"""Overlap window invariants on inert synthetic text and the ingest worker."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR', str(Path(__file__).resolve().parents[2] / 'api')))
from services import chunker, ingest_pipeline


def recover(chunks, overlap):
    return chunks[0] + ''.join(c[overlap:] for c in chunks[1:]) if chunks else ''


class OverlapTests(unittest.TestCase):
    def check_windows(self, text, size=1000, overlap=200, minimum=100):
        chunks = chunker.chunk_overlap(text, size, overlap, minimum)
        self.assertEqual(recover(chunks, overlap), text)
        for left, right in zip(chunks, chunks[1:]):
            if overlap:
                self.assertEqual(left[-overlap:], right[:overlap])
        self.assertTrue(all(len(c) <= size for c in chunks[:-1]))
        # One short final window can extend its predecessor by its new suffix.
        bound = size + max(0, min(size, minimum - 1) - overlap)
        self.assertTrue(all(len(c) <= bound for c in chunks))
        return chunks

    def test_long_token_has_bounded_windows_and_exact_coverage(self):
        chunks = self.check_windows('x' * 10000)
        self.assertEqual(len(chunks), 13)
        self.assertEqual(len(chunks[-1]), 400)

    def test_parser_single_newlines_preserve_order_and_overlap(self):
        self.check_windows('\n'.join('synthetic line ' + str(i) for i in range(600)))

    def test_paragraph_separators_and_unicode_are_preserved(self):
        self.check_windows('\n\n'.join('Paragraph ' + str(i) + ' café 🌿 ' * 80 for i in range(30)))

    def test_short_tail_merges_only_new_suffix_with_documented_bound(self):
        chunks = self.check_windows(''.join(chr(65 + i % 26) for i in range(1020)), overlap=10)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 1020)

    def test_zero_overlap_tail_preserves_exact_text_without_added_separator(self):
        self.assertEqual(len(self.check_windows('x' * 1040, overlap=0)[0]), 1040)

    def test_minimum_larger_than_split_target_keeps_the_tail_policy_bounded(self):
        chunks = self.check_windows('x'*10000,minimum=2000)
        self.assertEqual(len(chunks[-1]),1200)
        self.assertEqual(len(chunks),12)

    def test_tail_at_minimum_remains_separate(self):
        chunks = self.check_windows('x' * 1080, overlap=20)
        self.assertEqual([len(c) for c in chunks], [1000, 100])

    def test_document_shorter_than_minimum_is_not_padded(self):
        self.assertEqual(self.check_windows('short'), ['short'])

    def test_exact_window_and_near_total_overlap_do_not_emit_redundant_tail(self):
        self.assertEqual(self.check_windows('x' * 1000), ['x' * 1000])
        self.assertEqual([len(c) for c in self.check_windows('x' * 1001, overlap=999)], [1000, 1000])

    def test_empty_or_whitespace_only_input_has_no_chunks(self):
        for text in ('', ' \n\t '):
            self.assertEqual(chunker.chunk_overlap(text, 1000, 200, 100), [])

    def test_internal_and_boundary_whitespace_is_preserved(self):
        self.check_windows('   a' + ' ' * 2500 + 'b\n\t')

    def test_invalid_sizes_fail_before_splitting(self):
        for size, overlap, minimum in ((0,0,0),(-1,0,0),(10,-1,0),(10,10,0),
                                      (10,11,0),(10,0,-1),(True,0,0),
                                      (10,False,0),(10,0,True),(10.5,0,0)):
            with self.subTest(values=(size,overlap,minimum)), self.assertRaises(ValueError):
                chunker.chunk_overlap('inert',size,overlap,minimum)

    def test_entrypoint_uses_the_bounded_overlap_strategy(self):
        text = 'entrypoint' * 1000
        self.assertEqual(recover(chunker.chunk(text, 'overlap', 1000, 200), 200), text)

    def test_small_parameter_matrix_preserves_coverage_and_stated_bound(self):
        for size in range(1, 16):
            for overlap in range(size):
                for minimum in (0, 1, size, size+1, 2*size+1):
                    for length in (1, size, size+1, 2*size-1, 2*size+3):
                        text = ''.join(chr(65 + i % 26) for i in range(length))
                        with self.subTest(size=size, overlap=overlap, minimum=minimum, length=length):
                            self.check_windows(text,size,overlap,minimum)

    def test_ingest_worker_stores_bounded_overlapping_windows(self):
        text = '\n'.join('Inert source line ' + str(i) for i in range(400))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'inert.txt'; source.write_text(text)
            job_id = 'overlap-controlled'
            job = {'status':'queued', 'files_completed':0, 'files_failed':0, 'chunks_stored':0, 'errors':[], 'files_total':1}
            with patch.dict(ingest_pipeline._jobs, {job_id:job}, clear=True), \
                 patch.object(ingest_pipeline, '_parse_file', return_value=(text,None)), \
                 patch.object(ingest_pipeline.wc, '_insert_chunks_sync') as store, \
                 patch.object(ingest_pipeline.sources, 'store'):
                ingest_pipeline._process_job_sync(job_id,[source],Path(directory),'ReviewOverlap','overlap',1000,200,0.85,100)
            self.assertEqual(job['status'], 'completed', job)
            saved = store.call_args.args[1]
            self.assertTrue(all(len(c['content']) <= 1000 for c in saved))
            self.assertEqual(recover([c['content'] for c in saved],200),text)
            self.assertEqual([c['chunk_index'] for c in saved],list(range(len(saved))))


class ImplementationTests(unittest.TestCase):
    def test_embedded_changed_sources_match_runtime(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / 'IMPLEMENTATION.md').read_text()
        for name,fence,language in [('api/services/chunker.py','```','python'),
                                     ('scripts/verify/overlap_chunks.py','```','python'),
                                     ('scripts/verify/README.md','````','markdown')]:
            with self.subTest(file=name):
                header = '### ' + name + '\n\n' + fence + language + '\n'
                start = text.index(header) + len(header)
                end = text.index('\n' + fence + '\n',start)
                self.assertEqual(text[start:end], (root/name).read_text().rstrip('\n'))


if __name__ == '__main__': unittest.main()
