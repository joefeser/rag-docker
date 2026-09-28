"""Real parser/ingest/Weaviate overlap acceptance on one owned collection.

Run inside a disposable API with its embedding model already present:
python - < scripts/verify/overlap_chunks.py
Only this script's unique collection and temporary source/config paths are used.
"""
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch
from config import settings
from services import chunker, ingest_pipeline, weaviate_client as wc

collection = 'VfyOverlap' + uuid.uuid4().hex[:12]
assert not wc._collection_exists_sync(collection)
created = False
try:
    wc._create_collection_sync(collection, 'hnsw', 'cosine', {})
    created = True
    print('PASS owned overlap collection created', flush=True)
    with tempfile.TemporaryDirectory(prefix='overlap-live-') as directory:
        root = Path(directory)
        cases = [('long_token', 'x' * 10000, 200, 100),
                 ('single_newlines', '\n'.join('Inert source line ' + str(i) for i in range(600)), 200, 100),
                 ('paragraphs', '\n\n'.join('Inert paragraph ' + str(i) + ' abc' * 80 for i in range(20)), 200, 100),
                 ('short_tail', 'z' * 1020, 10, 100)]
        with patch.object(settings, 'sources_dir', str(root/'retained')):
            for label, text, overlap, minimum in cases:
                stage = root/label; stage.mkdir()
                source = stage/(label + '.txt'); source.write_text(text)
                parsed, _ = ingest_pipeline._parse_file(source)
                expected = chunker.chunk_overlap(parsed,1000,overlap,minimum)
                job_id = 'overlap-' + uuid.uuid4().hex[:8]
                job = {'status':'queued', 'files_total':1, 'files_completed':0, 'files_failed':0,
                       'chunks_stored':0, 'errors':[]}
                ingest_pipeline._jobs[job_id] = job
                try:
                    ingest_pipeline._process_job_sync(job_id,[source],stage,collection,'overlap',1000,overlap,0.85,minimum)
                    assert job['status'] == 'completed' and job['files_failed'] == 0, job
                    objects = wc.get_client().collections.get(collection).iterator()
                    saved = sorted((o.properties for o in objects if o.properties['source_file'] == source.name),
                                   key=lambda p:p['chunk_index'])
                    chunks = [p['content'] for p in saved]
                    assert chunks == expected and job['chunks_stored'] == len(chunks), (label,job)
                    restored = chunks[0] + ''.join(c[overlap:] for c in chunks[1:]) if chunks else ''
                    assert restored == parsed, label
                    assert all(len(c) <= 1000 for c in chunks[:-1])
                    assert all(len(c) <= 1000 + max(0,min(1000,minimum-1)-overlap) for c in chunks)
                    assert all(a[-overlap:] == b[:overlap] for a,b in zip(chunks,chunks[1:]))
                    print(f'PASS {label}: real parsed text stored as {len(chunks)} bounded windows with exact coverage/overlap', flush=True)
                finally:
                    ingest_pipeline._jobs.pop(job_id,None)
    print('PASS owned temporary source paths removed', flush=True)
finally:
    if created:
        wc._delete_collection_sync(collection)
    wc.close_client()
assert not wc._collection_exists_sync(collection)
wc.close_client()
print('PASS owned overlap collection and its configuration removed', flush=True)
