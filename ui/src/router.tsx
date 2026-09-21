import { Routes, Route, Navigate } from 'react-router-dom'
import { useRole } from './context/RoleContext'
import NavBar from './components/NavBar'
import LandingPage from './pages/LandingPage'
import QAPage from './pages/QAPage'
import ImportPage from './pages/ImportPage'
import ChunkingPage from './pages/ChunkingPage'
import RetrievalPage from './pages/RetrievalPage'
import GoldStandardPage from './pages/GoldStandardPage'
import CollectionsPage from './pages/CollectionsPage'
import HealthPage from './pages/HealthPage'
import TransferPage from './pages/TransferPage'
import HelpTransferPage from './pages/HelpTransferPage'

export default function AppRouter() {
  const { role } = useRole()

  if (!role) {
    return (
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    )
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <NavBar />
      <main className="max-w-6xl mx-auto px-4 py-6">
        <Routes>
          <Route path="/" element={<Navigate to="/qa" replace />} />
          <Route path="/qa" element={<QAPage />} />
          {role !== 'end_user' && (
            <>
              <Route path="/import" element={<ImportPage />} />
              <Route path="/chunking" element={<ChunkingPage />} />
              <Route path="/retrieval" element={<RetrievalPage />} />
              <Route path="/goldstandard" element={<GoldStandardPage />} />
              <Route path="/transfer" element={<TransferPage />} />
              <Route path="/help/transfer" element={<HelpTransferPage />} />
            </>
          )}
          {role === 'engineer' && (
            <>
              <Route path="/collections" element={<CollectionsPage />} />
              <Route path="/health" element={<HealthPage />} />
            </>
          )}
          <Route path="*" element={<Navigate to="/qa" replace />} />
        </Routes>
      </main>
    </div>
  )
}
