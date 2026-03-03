import { Copy, Check, Skull, Trash2, Terminal, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { SessionInfo, ConversationView as ConversationViewType } from '../lib/types'
import { parseJsonlContent } from '../lib/parsers'
import ConversationViewer from './ConversationView'

function formatDuration(session: SessionInfo): string {
  const end = session.end_time || Date.now() / 1000
  const seconds = Math.floor(end - session.start_time)
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  const hours = Math.floor(seconds / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  return `${hours}h ${mins}m`
}

const statusStyles: Record<string, { label: string; color: string }> = {
  running: { label: 'Running', color: 'var(--accent)' },
  done: { label: 'Complete', color: 'var(--text-muted)' },
  failed: { label: 'Failed', color: 'var(--red)' },
}

export default function SessionDetail({ session, onKill, onDelete }: {
  session: SessionInfo
  onKill: (id: string) => void
  onDelete: (id: string) => void
}) {
  const [conversation, setConversation] = useState<ConversationViewType | null>(null)
  const [logError, setLogError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [tmuxCopied, setTmuxCopied] = useState(false)

  const fetchLog = useCallback(async () => {
    try {
      const res = await fetch(`/api/sessions/${session.id}/log`)
      if (!res.ok) {
        setConversation(null)
        setLogError(res.status === 404 ? 'No log file found' : `Error: ${res.status}`)
        return
      }
      const text = await res.text()
      if (!text.trim()) {
        setConversation(null)
        setLogError(null)
        return
      }
      const result = parseJsonlContent(text, session.id)
      setConversation(result.conversation)
      setLogError(result.error || null)
    } catch (err) {
      setLogError(String(err))
    } finally {
      setLoading(false)
    }
  }, [session.id])

  useEffect(() => {
    setLoading(true)
    setConversation(null)
    setLogError(null)
    fetchLog()

    // Auto-refresh if session is still running
    if (session.status === 'running') {
      const interval = setInterval(fetchLog, 3000)
      return () => clearInterval(interval)
    }
  }, [session.id, session.status, fetchLog])

  const copyTmuxCmd = useCallback(async () => {
    await navigator.clipboard.writeText(`tmux attach -t ${session.tmux_session}`)
    setTmuxCopied(true)
    setTimeout(() => setTmuxCopied(false), 2000)
  }, [session.tmux_session])

  const status = statusStyles[session.status] || statusStyles.done

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="shrink-0 border-b border-[var(--border)] px-5 py-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="mono text-sm text-[var(--text)] leading-snug mb-2">
              {session.name || session.prompt}
            </div>
            <div className="flex items-center gap-3 flex-wrap">
              <span className="mono text-[11px] font-medium" style={{ color: status.color }}>
                {status.label}
                {session.status === 'running' && <Loader2 className="inline w-3 h-3 ml-1 animate-spin" />}
              </span>
              <span className="mono text-[11px] text-[var(--text-dim)]">{session.provider}</span>
              {session.model && <span className="mono text-[11px] text-[var(--text-dim)]">{session.model}</span>}
              <span className="mono text-[11px] text-[var(--text-dim)]">{formatDuration(session)}</span>
              <span className="mono text-[10px] text-[var(--text-dim)]">{session.id}</span>
            </div>
            {session.cwd && (
              <div className="mono text-[10px] text-[var(--text-dim)] mt-1 truncate">{session.cwd}</div>
            )}
          </div>

          {/* Actions */}
          <div className="flex items-center gap-1 shrink-0">
            {session.status === 'running' && (
              <>
                <button onClick={copyTmuxCmd} className="flex items-center gap-1 px-2 py-1 rounded text-[11px] mono text-[var(--text-muted)] hover:text-[var(--text)] hover:bg-[var(--bg-surface)] transition-colors" title="Copy tmux attach command">
                  {tmuxCopied ? <Check className="w-3 h-3 text-[var(--accent)]" /> : <Terminal className="w-3 h-3" />}
                  <span>Attach</span>
                </button>
                <button onClick={() => onKill(session.id)} className="flex items-center gap-1 px-2 py-1 rounded text-[11px] mono text-[var(--red)]/70 hover:text-[var(--red)] hover:bg-[var(--red)]/5 transition-colors" title="Kill session">
                  <Skull className="w-3 h-3" />
                  <span>Kill</span>
                </button>
              </>
            )}
            {session.status !== 'running' && (
              <button onClick={() => onDelete(session.id)} className="flex items-center gap-1 px-2 py-1 rounded text-[11px] mono text-[var(--text-dim)] hover:text-[var(--red)] hover:bg-[var(--red)]/5 transition-colors" title="Delete session">
                <Trash2 className="w-3 h-3" />
                <span>Delete</span>
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Conversation */}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        {loading && (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="w-5 h-5 animate-spin text-[var(--text-dim)]" />
          </div>
        )}
        {!loading && logError && (
          <div className="mono text-xs text-[var(--text-dim)] py-8 text-center">{logError}</div>
        )}
        {!loading && !logError && !conversation && (
          <div className="mono text-xs text-[var(--text-dim)] py-8 text-center">
            {session.status === 'running' ? 'Waiting for output...' : 'No conversation data found.'}
          </div>
        )}
        {conversation && <ConversationViewer conversation={conversation} />}
      </div>
    </div>
  )
}
