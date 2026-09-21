import { useEffect, useState } from 'react'
import { api, CollectionInfo } from '../api/client'
import { useRole } from '../context/RoleContext'
import { useQueryConfig, QueryConfig } from '../context/QueryConfigContext'

const MODES = [
  { id: 'hnsw', label: 'HNSW — Approximate (default)', description: 'The fastest option. Uses a smart graph to find the closest matches quickly. May very rarely miss the single best result, but works well for almost all use cases.' },
  { id: 'flat', label: 'Flat — Exact', description: 'Checks every stored chunk to find the mathematically perfect match. More accurate but slower as your collection grows. Best for collections under 10,000 chunks.' },
  { id: 'hybrid', label: 'Hybrid', description: 'Combines keyword search with meaning-based search. Best when your questions include specific terms, names, or codes. Adjust the slider to balance between the two modes.' },
  { id: 'semantic', label: 'Semantic', description: 'Pure meaning-based search. Best for conceptual questions where the exact words are less important than the idea.' },
]

export default function RetrievalPage() {
  const { role } = useRole()
  const { collection, setCollection, config, isDefault, loading, error, saveConfig } = useQueryConfig()
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [mode, setMode] = useState(config.retrieval_mode)
  const [topK, setTopK] = useState(config.top_k)
  const [alpha, setAlpha] = useState(config.alpha)
  const [ef, setEf] = useState(config.ef ?? 64)
  const [efConstruction, setEfConstruction] = useState(128)
  const [maxConnections, setMaxConnections] = useState(64)
  const [applied, setApplied] = useState(false)
  const [saveError, setSaveError] = useState('')

  useEffect(() => {
    api.getCollections().then(r => {
      setCollections(r.collections)
      if (!collection && r.collections.length > 0) setCollection(r.collections[0].name)
    }).catch(() => {})
    // Runs once; picking a default collection must not fight the user's choice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Saved settings arrive asynchronously and change whenever another
  // collection is picked, so the form mirrors the context rather than owning
  // the values. `applied` is deliberately not reset here: a save replaces
  // `config`, which would otherwise clear the confirmation immediately.
  useEffect(() => {
    setMode(config.retrieval_mode)
    setTopK(config.top_k)
    setAlpha(config.alpha)
    setEf(config.ef ?? 64)
  }, [config])

  useEffect(() => {
    setApplied(false)
    setSaveError('')
  }, [collection])

  async function apply() {
    const next: QueryConfig = {
      retrieval_mode: mode,
      top_k: topK,
      alpha,
      ef: mode === 'hnsw' ? ef : null,
      // The role toggle drives the answer style live; persisting it here is
      // what makes the stored config usable by an exported retrieval script.
      response_format: role === 'end_user' ? 'end_user' : 'engineer',
    }
    setSaveError('')
    try {
      await saveConfig(next)
      setApplied(true)
      setTimeout(() => setApplied(false), 3000)
    } catch (e: unknown) {
      setSaveError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="max-w-xl mx-auto">
      <h1 className="text-2xl font-bold mb-2">Retrieval Configuration</h1>
      <p className="text-sm text-gray-500 mb-6">
        Settings are saved per collection on the server, so they survive a restart and travel with an export.
      </p>

      <div className="mb-4">
        <label className="block text-sm font-medium mb-2">Collection</label>
        <select
          value={collection}
          onChange={e => setCollection(e.target.value)}
          className="w-full border rounded px-3 py-2 text-sm"
        >
          {collections.length === 0 && <option value="">No collections yet</option>}
          {collections.map(c => (
            <option key={c.name} value={c.name}>{c.name} ({c.object_count} chunks)</option>
          ))}
        </select>
        <p className="text-xs text-gray-500 mt-1">
          {loading
            ? 'Loading saved settings…'
            : collection
              ? isDefault
                ? 'No settings saved for this collection yet — showing defaults.'
                : 'Showing the settings saved for this collection.'
              : 'Create a collection to configure retrieval.'}
        </p>
        {error && <p className="text-xs text-amber-600 mt-1">Could not load saved settings ({error}). Showing defaults.</p>}
      </div>

      <div className="mb-4">
        <label className="block text-sm font-medium mb-2">Retrieval Mode</label>
        <div className="space-y-2">
          {MODES.map(m => (
            <label key={m.id} className={`flex gap-3 p-3 border rounded cursor-pointer ${mode === m.id ? 'border-blue-500 bg-blue-50' : 'hover:bg-gray-50'}`}>
              <input type="radio" name="mode" value={m.id} checked={mode === m.id} onChange={() => setMode(m.id)} className="mt-1" />
              <div>
                <div className="text-sm font-medium">{m.label}</div>
                <div className="text-xs text-gray-500">{m.description}</div>
              </div>
            </label>
          ))}
        </div>
      </div>

      <div className="mb-4">
        <label className="block text-xs text-gray-600 mb-1">Top-K Results: {topK}</label>
        <input type="range" min={1} max={20} value={topK} onChange={e => setTopK(+e.target.value)} className="w-full" />
      </div>

      {mode === 'hybrid' && (
        <div className="mb-4">
          <label className="block text-xs text-gray-600 mb-1">
            Keyword ← Balance → Meaning: {alpha}
          </label>
          <input type="range" min={0} max={1} step={0.05} value={alpha} onChange={e => setAlpha(+e.target.value)} className="w-full" />
        </div>
      )}

      {mode === 'hnsw' && role === 'engineer' && (
        <details className="mb-4 border rounded">
          <summary className="px-3 py-2 text-sm cursor-pointer font-medium">Advanced HNSW Parameters</summary>
          <div className="p-3 space-y-3">
            <div>
              <label className="block text-xs text-gray-600 mb-1">ef (query accuracy): {ef}</label>
              <input type="range" min={16} max={512} step={8} value={ef} onChange={e => setEf(+e.target.value)} className="w-full" />
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">efConstruction (build accuracy): {efConstruction}</label>
              <input type="range" min={64} max={512} step={8} value={efConstruction} onChange={e => setEfConstruction(+e.target.value)} className="w-full" />
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">maxConnections (graph density): {maxConnections}</label>
              <input type="range" min={16} max={128} step={4} value={maxConnections} onChange={e => setMaxConnections(+e.target.value)} className="w-full" />
            </div>
          </div>
        </details>
      )}

      <button
        onClick={apply}
        disabled={!collection || loading}
        className="bg-blue-600 text-white px-5 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50"
      >
        Save for this collection
      </button>
      {applied && <span className="ml-3 text-green-600 text-sm">Saved!</span>}
      {saveError && <p className="text-red-600 text-sm mt-3">{saveError}</p>}
    </div>
  )
}
