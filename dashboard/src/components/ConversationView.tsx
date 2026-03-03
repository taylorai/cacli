import { Check, ChevronRight, Copy } from 'lucide-react'
import { useCallback, useState } from 'react'
import Markdown from 'react-markdown'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import type { ConversationView as ConversationViewType, MessageBlock, ResultSummary } from '../lib/types'

const clip = (value: string, size = 120) => {
  if (!value) return ''
  if (value.length <= size) return value
  return `${value.slice(0, size)}...`
}

// Extract embedded Python from shell commands
const extractPythonCode = (command: string): string | null => {
  const heredocMatch = command.match(/python3?\s+-\s*<<['"]?(\w+)['"]?\n([\s\S]*?)\n\1(?:\s|$)/)
  if (heredocMatch) return heredocMatch[2].trim()
  const dashCMatch = command.match(/python3?\s+-c\s+(['"])([\s\S]*?)\1/)
  if (dashCMatch) return dashCMatch[2].replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\'/g, "'").replace(/\\"/g, '"').trim()
  return null
}

const CopyButton = ({ text }: { text: string }) => {
  const [copied, setCopied] = useState(false)
  const handleCopy = useCallback(async () => {
    if (!text) return
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [text])

  return (
    <button onClick={handleCopy} className="p-1 rounded text-[var(--text-dim)] hover:text-[var(--text-muted)] transition-colors" title="Copy">
      {copied ? <Check className="w-3 h-3 text-[var(--accent)]" /> : <Copy className="w-3 h-3" />}
    </button>
  )
}

const MarkdownContent = ({ children }: { children: string }) => (
  <div className="mono text-xs leading-relaxed text-[var(--text)]">
    <Markdown
      components={{
        h1: ({ children }) => <h1 className="text-base font-semibold mt-4 mb-2 first:mt-0">{children}</h1>,
        h2: ({ children }) => <h2 className="text-sm font-semibold mt-3 mb-2 first:mt-0">{children}</h2>,
        h3: ({ children }) => <h3 className="text-xs font-semibold mt-3 mb-1 first:mt-0">{children}</h3>,
        p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
        strong: ({ children }) => <strong className="font-semibold text-[var(--text)]">{children}</strong>,
        em: ({ children }) => <em className="italic">{children}</em>,
        code: ({ className, children }) => {
          const match = /language-(\w+)/.exec(className || '')
          if (!className) return <code className="bg-white/5 rounded px-1 py-0.5 text-[0.9em] text-[var(--accent-dim)]">{children}</code>
          return (
            <SyntaxHighlighter language={match?.[1] || 'text'} style={oneDark} customStyle={{ margin: '0.5rem 0', padding: '0.5rem 0.75rem', fontSize: '0.7rem', borderRadius: '6px', background: '#1a1a1f' }}>
              {String(children).replace(/\n$/, '')}
            </SyntaxHighlighter>
          )
        },
        pre: ({ children }) => <>{children}</>,
        ul: ({ children }) => <ul className="list-disc list-inside mb-2 ml-2">{children}</ul>,
        ol: ({ children }) => <ol className="list-decimal list-inside mb-2 ml-2">{children}</ol>,
        li: ({ children }) => <li className="mb-0.5">{children}</li>,
        blockquote: ({ children }) => <blockquote className="border-l-2 border-[var(--text-dim)] pl-3 my-2 text-[var(--text-muted)] italic">{children}</blockquote>,
        a: ({ href, children }) => <a href={href} className="text-[var(--accent-dim)] underline hover:text-[var(--accent)]" target="_blank" rel="noopener noreferrer">{children}</a>,
        hr: () => <hr className="my-3 border-[var(--border)]" />,
      }}
    >
      {children}
    </Markdown>
  </div>
)

const BlockRenderer = ({ block }: { block: MessageBlock }) => {
  const [expanded, setExpanded] = useState(false)

  if (block.type === 'tool_use' || block.type === 'tool_call') {
    const toolName = block.json && typeof block.json === 'object' && 'name' in block.json ? (block.json as { name?: string }).name : block.text
    let toolInput: unknown = null
    if (block.json && typeof block.json === 'object') {
      if ('input' in block.json) toolInput = (block.json as { input?: unknown }).input
      else if ('arguments' in block.json) {
        const args = (block.json as { arguments?: string }).arguments
        try { toolInput = args ? JSON.parse(args) : null } catch { toolInput = args }
      }
    }

    const shellCommand = toolInput && typeof toolInput === 'object' && 'command' in toolInput ? (toolInput as { command?: string }).command : null
    const pythonCode = shellCommand ? extractPythonCode(shellCommand) : null
    const inputPreview = toolInput ? JSON.stringify(toolInput) : ''

    return (
      <div className="rounded border border-[var(--border)] bg-[var(--bg)]/50 overflow-hidden">
        <button onClick={() => setExpanded(!expanded)} className="w-full flex items-center gap-1.5 px-2 py-1 text-left hover:bg-white/[0.02] transition">
          <ChevronRight className={`w-3 h-3 text-[var(--text-dim)] transition-transform ${expanded ? 'rotate-90' : ''}`} />
          <code className="mono text-xs text-[var(--text-muted)]">
            {toolName}({expanded ? '' : clip(inputPreview, 60)})
          </code>
        </button>
        {expanded && Boolean(toolInput) && (
          <div className="border-t border-[var(--border)]">
            {pythonCode ? (
              <SyntaxHighlighter language="python" style={oneDark} customStyle={{ margin: 0, padding: '0.5rem 0.75rem', fontSize: '0.7rem', background: '#1a1a1f' }}>
                {pythonCode}
              </SyntaxHighlighter>
            ) : (
              <pre className="px-3 py-2 mono text-xs text-[var(--text-muted)] whitespace-pre-wrap overflow-x-auto">
                {typeof toolInput === 'string' ? toolInput : JSON.stringify(toolInput, null, 2)}
              </pre>
            )}
          </div>
        )}
      </div>
    )
  }

  if (block.type === 'tool_result') {
    const text = block.text || '-'
    const lines = text.split('\n')
    const isLong = lines.length > 8

    return (
      <div className="rounded border border-[var(--border)] bg-[var(--bg)]/50 overflow-hidden">
        {isLong ? (
          <>
            <button onClick={() => setExpanded(!expanded)} className="w-full flex items-center gap-1.5 px-2 py-1 text-left hover:bg-white/[0.02] transition">
              <ChevronRight className={`w-3 h-3 text-[var(--text-dim)] transition-transform ${expanded ? 'rotate-90' : ''}`} />
              <span className="mono text-xs text-[var(--text-dim)]">Result ({lines.length} lines)</span>
            </button>
            {expanded && <pre className="px-3 py-2 mono text-[11px] text-[var(--text-muted)] whitespace-pre-wrap border-t border-[var(--border)] max-h-96 overflow-y-auto">{text}</pre>}
          </>
        ) : (
          <pre className="px-3 py-2 mono text-[11px] text-[var(--text-muted)] whitespace-pre-wrap max-h-96 overflow-y-auto">{text}</pre>
        )}
      </div>
    )
  }

  if (block.type === 'thinking') {
    return (
      <div className="rounded border border-[var(--blue)]/20 bg-[var(--blue)]/5 overflow-hidden">
        <button onClick={() => setExpanded(!expanded)} className="w-full flex items-center gap-1.5 px-2 py-1 text-left hover:bg-[var(--blue)]/10 transition">
          <ChevronRight className={`w-3 h-3 text-[var(--blue)]/50 transition-transform ${expanded ? 'rotate-90' : ''}`} />
          <span className="mono text-xs text-[var(--blue)]/70">Thinking...</span>
        </button>
        {expanded && <pre className="px-3 py-2 mono text-[11px] text-[var(--text-muted)] whitespace-pre-wrap border-t border-[var(--blue)]/20 max-h-96 overflow-y-auto">{block.text}</pre>}
      </div>
    )
  }

  // Default: text block
  if (block.text) return <MarkdownContent>{block.text}</MarkdownContent>
  return null
}

const roleMeta: Record<string, { label: string; accent: string; bg: string; border: string }> = {
  user: { label: 'User', accent: 'var(--amber)', bg: 'rgba(251,191,36,0.05)', border: 'rgba(251,191,36,0.15)' },
  assistant: { label: 'Assistant', accent: 'var(--accent)', bg: 'rgba(110,231,183,0.04)', border: 'rgba(110,231,183,0.12)' },
  tool: { label: 'Tool', accent: 'var(--text-dim)', bg: 'transparent', border: 'var(--border-subtle)' },
  system: { label: 'System', accent: 'var(--blue)', bg: 'rgba(96,165,250,0.05)', border: 'rgba(96,165,250,0.15)' },
}

const Bubble = ({ role, blocks }: { role: string; blocks?: MessageBlock[] | null }) => {
  const meta = roleMeta[role] || roleMeta.assistant
  const copyText = blocks?.map(b => b.text || '').filter(Boolean).join('\n\n') || ''

  return (
    <div className="rounded-lg border overflow-hidden" style={{ background: meta.bg, borderColor: meta.border }}>
      <div className="flex items-center justify-between px-3 py-1.5">
        <span className="mono text-[10px] font-medium uppercase tracking-wider" style={{ color: meta.accent }}>{meta.label}</span>
        {copyText && <CopyButton text={copyText} />}
      </div>
      {blocks && blocks.length > 0 && (
        <div className="px-3 pb-3 flex flex-col gap-2">
          {blocks.map((block, i) => <BlockRenderer key={i} block={block} />)}
        </div>
      )}
    </div>
  )
}

const ResultBanner = ({ summary }: { summary: ResultSummary }) => {
  const [showDetails, setShowDetails] = useState(false)
  const isError = summary.is_error
  const durationSec = (summary.duration_ms / 1000).toFixed(1)
  const cost = summary.total_cost_usd

  return (
    <div className={`rounded-lg border p-3 ${isError ? 'border-[var(--red)]/30 bg-[var(--red)]/5' : 'border-[var(--accent)]/20 bg-[var(--accent)]/5'}`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className={`mono text-xs font-medium ${isError ? 'text-[var(--red)]' : 'text-[var(--accent)]'}`}>
            {isError ? 'Error' : 'Complete'}
          </span>
          <span className="mono text-[11px] text-[var(--text-muted)]">{durationSec}s</span>
          {summary.num_turns > 0 && <span className="mono text-[11px] text-[var(--text-dim)]">{summary.num_turns} turns</span>}
          {cost !== undefined && <span className="mono text-[11px] text-[var(--text-dim)]">${cost.toFixed(4)}</span>}
        </div>
        {summary.usage && (
          <button onClick={() => setShowDetails(!showDetails)} className="text-[10px] text-[var(--text-dim)] hover:text-[var(--text-muted)] transition-colors">
            {showDetails ? 'Hide' : 'Details'}
          </button>
        )}
      </div>
      {showDetails && summary.usage && (
        <pre className="mt-2 mono text-[11px] text-[var(--text-dim)] whitespace-pre-wrap">{JSON.stringify(summary.usage, null, 2)}</pre>
      )}
    </div>
  )
}

export default function ConversationViewer({ conversation }: { conversation: ConversationViewType }) {
  return (
    <div className="flex flex-col gap-2">
      {conversation.messages.map((msg, i) => (
        <Bubble key={i} role={msg.role} blocks={msg.blocks} />
      ))}
      {conversation.resultSummary && <ResultBanner summary={conversation.resultSummary} />}
    </div>
  )
}
