import { useCallback, useEffect, useRef, useState } from 'react'
import type { SessionInfo } from './lib/types'
import SessionList from './components/SessionList'
import SessionDetail from './components/SessionDetail'
import { RefreshCw } from 'lucide-react'

export default function App() {
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const hasRunningRef = useRef(false)

  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch('/api/sessions')
      if (!res.ok) throw new Error(`${res.status}`)
      const data: SessionInfo[] = await res.json()
      setSessions(data)
      hasRunningRef.current = data.some(s => s.status === 'running')
      setError(null)
    } catch (err) {
      setError(`Cannot reach server: ${err}`)
    }
  }, [])

  useEffect(() => {
    fetchSessions()
    const interval = setInterval(fetchSessions, 3000)
    return () => clearInterval(interval)
  }, [fetchSessions])

  // Auto-select first session if none selected
  useEffect(() => {
    if (!selectedId && sessions.length) {
      setSelectedId(sessions[0].id)
    }
  }, [sessions, selectedId])

  const selectedSession = sessions.find(s => s.id === selectedId) || null

  const handleKill = useCallback(async (id: string) => {
    await fetch(`/api/sessions/${id}/kill`, { method: 'POST' })
    fetchSessions()
  }, [fetchSessions])

  const handleDelete = useCallback(async (id: string) => {
    await fetch(`/api/sessions/${id}`, { method: 'DELETE' })
    if (selectedId === id) setSelectedId(null)
    fetchSessions()
  }, [selectedId, fetchSessions])

  const runningCount = sessions.filter(s => s.status === 'running').length

  return (
    <div className="h-screen flex flex-col bg-[var(--bg)]">
      {/* Top bar */}
      <header className="shrink-0 h-10 flex items-center justify-between px-4 border-b border-[var(--border)]">
        <div className="flex items-center gap-3">
          <span className="mono text-xs font-semibold text-[var(--text)] tracking-wide">cacli</span>
          <span className="mono text-[10px] text-[var(--text-dim)]">{sessions.length} session{sessions.length !== 1 ? 's' : ''}</span>
          {runningCount > 0 && (
            <span className="mono text-[10px] text-[var(--accent)]">{runningCount} running</span>
          )}
        </div>
        <button onClick={fetchSessions} className="p-1 rounded text-[var(--text-dim)] hover:text-[var(--text-muted)] transition-colors" title="Refresh">
          <RefreshCw className="w-3.5 h-3.5" />
        </button>
      </header>

      {error && (
        <div className="shrink-0 px-4 py-2 bg-[var(--red)]/10 border-b border-[var(--red)]/20">
          <span className="mono text-xs text-[var(--red)]">{error}</span>
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 flex min-h-0">
        {/* Sidebar */}
        <aside className="w-80 shrink-0 border-r border-[var(--border)] overflow-y-auto bg-[var(--bg)]">
          <SessionList sessions={sessions} selectedId={selectedId} onSelect={setSelectedId} />
        </aside>

        {/* Detail */}
        <main className="flex-1 min-w-0 bg-[var(--bg-raised)]">
          {selectedSession ? (
            <SessionDetail session={selectedSession} onKill={handleKill} onDelete={handleDelete} />
          ) : (
            <div className="flex items-center justify-center h-full">
              <div className="text-center">
                <div className="mono text-xs text-[var(--text-dim)] mb-1">No session selected</div>
                <div className="mono text-[10px] text-[var(--text-dim)]">Select a session from the sidebar or spawn one with <code className="text-[var(--accent-dim)]">cacli spawn</code></div>
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  )
}
