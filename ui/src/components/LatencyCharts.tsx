import { LineChart, Line, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import { MetricsResult } from '../api/client'

export default function LatencyCharts({ data }: { data: MetricsResult }) {
  const chartData = data.history.map(r => ({
    time: new Date(r.timestamp).toLocaleTimeString(),
    Retrieval: r.retrieval_ms,
    LLM: r.llm_ms,
    Total: r.total_ms,
  }))

  return (
    <div>
      <div className="grid grid-cols-3 gap-4 mb-6">
        {([
          ['RETRIEVAL', data.retrieval_latency],
          ['LLM', data.llm_latency],
          ['TOTAL', data.total_latency],
        ] as const).map(([label, stats]) => (
          <div key={label} className="border rounded p-3 bg-white">
            <div className="text-xs text-gray-500 mb-1">{label}</div>
            <div className="text-sm">
              <span className="text-gray-600">P50:</span> {Math.round(stats.p50)}ms ·{' '}
              <span className="text-gray-600">P95:</span> {Math.round(stats.p95)}ms ·{' '}
              <span className="text-gray-600">P99:</span> {Math.round(stats.p99)}ms
            </div>
          </div>
        ))}
      </div>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={chartData}>
          <XAxis dataKey="time" tick={{ fontSize: 11 }} />
          <YAxis unit="ms" tick={{ fontSize: 11 }} />
          <Tooltip />
          <Legend />
          <Line type="monotone" dataKey="Retrieval" stroke="#3b82f6" dot={false} />
          <Line type="monotone" dataKey="LLM" stroke="#f59e0b" dot={false} />
          <Line type="monotone" dataKey="Total" stroke="#10b981" dot={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
