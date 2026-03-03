import { Circle } from 'lucide-react'
import type { SessionInfo } from '../lib/types'

function formatRuntime(session: SessionInfo): string {
  const end = session.end_time || Date.now() / 1000
  const seconds = Math.floor(end - session.start_time)
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  const hours = Math.floor(seconds / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  return `${hours}h ${mins}m`
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000)
  const now = new Date()
  const isToday = d.toDateString() === now.toDateString()
  if (isToday) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

const statusColor: Record<string, string> = {
  running: 'text-[var(--accent)] animate-pulse',
  done: 'text-[var(--text-dim)]',
  failed: 'text-[var(--red)]',
}

export default function SessionList({ sessions, selectedId, onSelect }: {
  sessions: SessionInfo[]
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  if (!sessions.length) {
    return (
      <div className="flex items-center justify-center h-full text-[var(--text-dim)] mono text-xs">
        No sessions yet.
        <br />
        Run <code className="text-[var(--accent-dim)]">cacli spawn "..."</code> to start.
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      {sessions.map((session) => {
        const isSelected = session.id === selectedId
        const prompt = session.name || session.prompt
        const clipped = prompt.length > 80 ? prompt.slice(0, 80) + '...' : prompt

        return (
          <button
            key={session.id}
            onClick={() => onSelect(session.id)}
            className={`w-full text-left px-3 py-2.5 border-b border-[var(--border-subtle)] transition-colors ${
              isSelected ? 'bg-[var(--bg-surface)]' : 'hover:bg-[var(--bg-raised)]'
            }`}
          >
            <div className="flex items-start gap-2">
              <Circle className={`w-2 h-2 mt-1.5 fill-current shrink-0 ${statusColor[session.status] || statusColor.done}`} />
              <div className="flex-1 min-w-0">
                <div className="mono text-xs text-[var(--text)] truncate">{clipped}</div>
                <div className="flex items-center gap-2 mt-1">
                  <span className="mono text-[10px] text-[var(--text-dim)]">{session.provider}</span>
                  {session.model && <span className="mono text-[10px] text-[var(--text-dim)]">{session.model}</span>}
                  <span className="mono text-[10px] text-[var(--text-dim)]">{formatRuntime(session)}</span>
                  <span className="mono text-[10px] text-[var(--text-dim)] ml-auto">{formatTime(session.start_time)}</span>
                </div>
              </div>
            </div>
          </button>
        )
      })}
    </div>
  )
}
