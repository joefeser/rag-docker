import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api/client'

export default function HelpTransferPage() {
  const [markdown, setMarkdown] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.getTransferHelp()
      .then(r => setMarkdown(r.markdown))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="max-w-3xl mx-auto">
      <div className="flex items-baseline justify-between mb-4">
        <h1 className="text-2xl font-bold">Export and Import</h1>
        <Link to="/transfer" className="text-sm text-blue-600 hover:underline">Go to Transfer →</Link>
      </div>
      {loading && <p className="text-sm text-gray-500">Loading…</p>}
      {error && (
        <p className="text-sm text-red-600">
          Could not load the help content ({error}). The API may be starting up.
        </p>
      )}
      {markdown && (
        // Served by the API, rendered from the same templates as the README
        // inside every package, so this page cannot drift from what ships.
        <article className="prose prose-sm max-w-none">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
        </article>
      )}
    </div>
  )
}
