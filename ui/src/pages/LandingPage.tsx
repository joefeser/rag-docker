import { useNavigate } from 'react-router-dom'
import { useRole } from '../context/RoleContext'

const ROLES = [
  {
    id: 'engineer' as const,
    label: 'AI Engineer',
    description: 'Full access: ingest, chunking, retrieval, gold standard, collection management, health dashboard.',
  },
  {
    id: 'developer' as const,
    label: 'Developer',
    description: 'Ingest documents, configure chunking and retrieval, run Q&A, generate gold standard.',
  },
  {
    id: 'end_user' as const,
    label: 'End User',
    description: 'Ask questions and get answers from the knowledge base.',
  },
]

export default function LandingPage() {
  const { setRole } = useRole()
  const navigate = useNavigate()

  function select(role: 'engineer' | 'developer' | 'end_user') {
    setRole(role)
    navigate('/qa')
  }

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-8">
      <h1 className="text-3xl font-bold text-gray-800 mb-2">RAG Platform</h1>
      <p className="text-gray-500 mb-10">Select your role to continue</p>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 w-full max-w-3xl">
        {ROLES.map(r => (
          <button
            key={r.id}
            onClick={() => select(r.id)}
            className="bg-white border-2 border-gray-200 hover:border-blue-500 rounded-xl p-6 text-left transition-all shadow-sm hover:shadow-md"
          >
            <h2 className="text-lg font-semibold text-gray-800 mb-2">{r.label}</h2>
            <p className="text-sm text-gray-500">{r.description}</p>
          </button>
        ))}
      </div>
    </div>
  )
}
