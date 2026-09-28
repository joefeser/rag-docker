"""Real SDK sampling checks; owned synthetic objects with supplied vectors.

Run inside a disposable API: python - < scripts/verify/chunk_sampling.py
Does not call embedding or language models. UUID selection, not answer output,
is the reproducibility contract. Deletes only its unique collection/config.
"""
import uuid
from services import weaviate_client as wc
from services.chunk_sampling import select_chunks

collection='VfySampling'+uuid.uuid4().hex[:12]
created=False
try:
    assert not wc._collection_exists_sync(collection)
    wc._create_collection_sync(collection,'hnsw','cosine',{})
    created=True
    coll=wc.get_client().collections.get(collection)
    for i in range(1,61):
        coll.data.insert(uuid=uuid.UUID(int=i),properties={
            'content':f'Inert sampling fixture {i}', 'source_file':'sampling.txt',
            'chunk_index':i},vector=[0.1]*768)
    assert coll.aggregate.over_all(total_count=True).total_count==60
    print('PASS created 60 owned synthetic objects with supplied vectors',flush=True)
    first=wc._sample_chunks_sync(collection,5,7)
    assert len(first)==5 and len({r['object_id'] for r in first})==5
    assert any(uuid.UUID(r['object_id']).int>5 for r in first)
    print('PASS seeded sample reaches beyond the first five objects',flush=True)
    assert wc._sample_chunks_sync(collection,5,7)==first
    print('PASS repeated seed preserves UUIDs and ordered payloads',flush=True)
    snapshot=list(coll.iterator(include_vector=False,return_properties=['content','source_file','chunk_index'],cache_size=100))
    assert select_chunks(reversed(snapshot),5,7)==first
    print('PASS reversed actual SDK objects preserve seeded selection',flush=True)
    rows=wc._sample_chunks_sync(collection,100,7)
    assert len(rows)==60 and {r['object_id'] for r in rows}=={str(uuid.UUID(int=i)) for i in range(1,61)}
    print('PASS oversize request returns all available unique objects',flush=True)
    unseeded=wc._sample_chunks_sync(collection,5,None)
    assert len(unseeded)==5 and all(1<=uuid.UUID(r['object_id']).int<=60 for r in unseeded)
    print('PASS null seed produces a bounded valid sample',flush=True)
finally:
    if created: wc._delete_collection_sync(collection)
    wc.close_client()
assert not wc._collection_exists_sync(collection)
wc.close_client()
print('PASS owned collection and configuration removed',flush=True)
