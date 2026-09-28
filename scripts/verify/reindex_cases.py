"""Controlled reindex preservation/failure cases; registered by14_reindex.sh."""
import copy, math, os, sys, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR', str(Path(__file__).resolve().parents[2]/'api') if __file__ != '<stdin>' else '/app'))
from services import tuning


def records():
    return [{'id': '49000000-0000-4000-8000-00000000000'+str(i),
             'vector': [0.125, float(i), -0.25],
             'properties': {'content': 'Owned inert '+str(i), 'chunk_index': i, 'source_file': 'owned.txt'}} for i in range(2)]


class Batch:
    def __init__(self, owner, name):
        self.owner, self.name, self.number_errors, self.pending = owner, name, 0, []
    def __enter__(self): return self
    def add_object(self, properties, uuid=None, vector=None):
        self.pending.append({'id': str(uuid), 'vector': copy.deepcopy(vector), 'properties': copy.deepcopy(properties)})
        if self.owner.mutate_arguments:
            properties['content'] = 'SDK argument mutation'; vector[0] = 999
    def __exit__(self, *exc):
        if self.owner.fail(self.name): self.number_errors = 1
        else: self.owner.data[self.name] += self.pending
        self.owner.closed.append(self.name)
        if self.owner.corrupt(self.name) and self.owner.data[self.name]:
            self.owner.data[self.name][0]['properties']['content'] = 'Owned readback corruption'
        if self.owner.change_source and self.name != 'OwnedReindex':
            self.owner.data['OwnedReindex'][0]['properties']['content'] = 'Newer independent write'


class Collections:
    def __init__(self, initial):
        self.data = {'OwnedReindex': copy.deepcopy(initial)}; self.deleted = []; self.created = []; self.closed = []
        self.fail = lambda name: False; self.corrupt = lambda name: False
        self.change_source = self.mutate_arguments = False
    def get(self, name):
        def iterator(include_vector=False):
            for record in self.data[name]:
                yield SimpleNamespace(uuid=record['id'], properties=copy.deepcopy(record['properties']), vector={'default':copy.deepcopy(record['vector'])} if include_vector else None)
        return SimpleNamespace(iterator=iterator, batch=SimpleNamespace(dynamic=lambda: Batch(self, name)))
    def delete(self, name): self.deleted.append(name); del self.data[name]
    def create(self, name, index, distance, hnsw):
        self.created.append((name,index,distance,copy.deepcopy(hnsw))); self.data[name] = []


class ReindexTests(unittest.TestCase):
    def setUp(self):
        self.original = records(); self.backend = Collections(self.original)
        self.config = {'index_type':'hnsw','distance_metric':'cosine','hnsw_config':{'ef':64,'efConstruction':128,'maxConnections':64}}
        self.embedding = Mock(side_effect=AssertionError('Reindex contacted embedding insertion'))
        self.stale = Mock(return_value=1)
        patches = [patch.object(tuning.wc,'get_client',return_value=SimpleNamespace(collections=self.backend)),
                   patch.object(tuning.wc,'_create_collection_sync',side_effect=self.backend.create),
                   patch.object(tuning.wc,'_collection_config_sync',return_value=self.config),
                   patch.object(tuning.wc,'_insert_chunks_sync',self.embedding),
                   patch.object(tuning.sources,'has_sources',return_value=False),
                   patch.object(tuning.goldstandard,'mark_stale',self.stale),
                   patch.object(tuning,'_jobs',{}),patch.object(tuning,'_active',{'OwnedReindex'})]
        for change in patches: change.start(); self.addCleanup(change.stop)
    def run_job(self, operation='reindex'):
        tuning._jobs['owned'] = {'status':'queued','chunks_written':0,'notes':[]}
        tuning._run('owned','OwnedReindex',operation,{'index_type':'flat','distance_metric':'dot'})
        return tuning._jobs['owned']
    def test_reindex_changes_physical_config_and_preserves_every_record_without_embedding(self):
        job=self.run_job(); self.assertEqual(job['status'],'completed'); self.assertEqual(job['chunks_written'],2)
        self.assertEqual(self.backend.data['OwnedReindex'],self.original); self.embedding.assert_not_called(); self.stale.assert_not_called()
        self.assertTrue(all(entry[1:3]==('flat','dot') for entry in self.backend.created)); self.assertIn('verified unchanged',job['notes'][0])
        self.assertNotIn('OwnedReindex',tuning._active)
    def test_deferred_batch_failure_is_seen_before_original_deletion(self):
        self.backend.fail=lambda name:name!='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertNotIn('OwnedReindex',self.backend.deleted)
        self.assertEqual(self.backend.data['OwnedReindex'],self.original); self.assertEqual(job['chunks_written'],0); self.stale.assert_not_called()
    def test_staging_readback_mismatch_preserves_original(self):
        self.backend.corrupt=lambda name:name!='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertNotIn('OwnedReindex',self.backend.deleted); self.stale.assert_not_called()
    def test_observed_source_change_during_staging_is_not_overwritten(self):
        self.backend.change_source=True; job=self.run_job(); self.assertEqual(job['status'],'failed')
        self.assertNotIn('OwnedReindex',self.backend.deleted); self.assertEqual(self.backend.data['OwnedReindex'][0]['properties']['content'],'Newer independent write')
    def test_final_deferred_failure_has_no_completion_claim_and_marks_retained_pairs(self):
        self.backend.fail=lambda name:name=='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertEqual(job['chunks_written'],0); self.assertEqual(job['notes'],[])
        self.stale.assert_called_once(); self.assertIn('cutover',self.stale.call_args.args[1])
    def test_final_readback_mismatch_is_not_completed(self):
        self.backend.corrupt=lambda name:name=='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertEqual(job['chunks_written'],0); self.stale.assert_called_once()
    def test_missing_or_nonfinite_vectors_fail_before_any_creation(self):
        for vector in [[],None,[math.nan],[math.inf],[True]]:
            with self.subTest(vector=vector):
                self.backend.data['OwnedReindex'][0]['vector']=vector; job=self.run_job()
                self.assertEqual(job['status'],'failed'); self.assertEqual(self.backend.created,[]); self.assertEqual(self.backend.deleted,[])
    def test_unsupported_named_vector_is_refused_without_guessing(self):
        col=SimpleNamespace(iterator=lambda **kw:iter([SimpleNamespace(uuid=self.original[0]['id'],properties={},vector={'other':[1.0]})]))
        with patch.object(tuning.wc,'get_client',return_value=SimpleNamespace(collections=SimpleNamespace(get=lambda name:col))):
            with self.assertRaisesRegex(RuntimeError,'single default vector'): tuning._existing_records('OwnedReindex')
    def test_duplicate_readback_id_is_refused(self):
        self.backend.data['OwnedReindex'].append(copy.deepcopy(self.original[0])); job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertEqual(self.backend.created,[])
    def test_sdk_argument_mutation_does_not_change_expected_snapshot(self):
        self.backend.mutate_arguments=True; snapshot=tuning._existing_records('OwnedReindex'); before=copy.deepcopy(snapshot)
        self.backend.create('OwnedCopy','flat','dot',{}); tuning._write_records('OwnedCopy',snapshot)
        self.assertEqual(snapshot,before); self.assertEqual(self.backend.data['OwnedCopy'],self.original)
    def test_empty_collection_reindexes_without_embedding(self):
        self.backend.data['OwnedReindex']=[]; job=self.run_job()
        self.assertEqual(job['status'],'completed'); self.assertEqual(job['chunks_written'],0); self.embedding.assert_not_called(); self.stale.assert_not_called()
    def test_reembed_retains_its_explicit_regeneration_path(self):
        def embed(name,props):
            self.backend.data[name]=[{'id':'49000000-0000-4000-8000-000000000100','vector':[4.,5.,6.],'properties':copy.deepcopy(props[0])},
                                     {'id':'49000000-0000-4000-8000-000000000101','vector':[4.,5.,6.],'properties':copy.deepcopy(props[1])}]
        self.embedding.side_effect=embed; job=self.run_job('reembed')
        self.assertEqual(job['status'],'completed'); self.embedding.assert_called_once(); self.stale.assert_called_once()
        self.assertNotEqual(self.backend.data['OwnedReindex'],self.original)

if __name__=='__main__': unittest.main()
