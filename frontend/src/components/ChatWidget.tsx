import { useState, useRef, useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  MessageSquare, X, Send, Loader2, Sparkles, ExternalLink, Wrench,
} from 'lucide-react';
import { getChatHistory, sendChat } from '@/api/endpoints';
import { pushToast } from '@/components/ui/Toast';
import { safeArr } from '@/lib/format';
import type { ChatMessage, ChatSource } from '@/types';
import ReactMarkdown from 'react-markdown';

export function ChatWidget({ jobId }: { jobId: string }) {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();

  const { data: history = [], isLoading: historyLoading } = useQuery({
    queryKey: ['chat-history', jobId],
    queryFn: () => getChatHistory(jobId),
    enabled: open,
    placeholderData: (prev) => prev,
    refetchOnWindowFocus: false,
  });

  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [localMsgs, setLocalMsgs] = useState<ChatMessage[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Merge server history with local optimistic messages
  const messages = [...safeArr(history), ...localMsgs];

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages.length, open]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput('');
    setSending(true);
    const optimistic: ChatMessage = { role: 'user', content: text };
    setLocalMsgs((prev) => [...prev, optimistic]);

    try {
      const res = await sendChat(jobId, text);

      setLocalMsgs((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: res.answer,
          ...(res.sources?.length ? { sources: res.sources } : {}),
          ...(res.tool_used ? { tool_used: res.tool_used } : {}),
        } as ChatMessage & { sources?: ChatSource[]; tool_used?: string },
      ]);

      // Server history now contains this conversation.
      // Fetch it, then remove the temporary local copies.
      await qc.invalidateQueries({ queryKey: ['chat-history', jobId] });
      setLocalMsgs([]);
    } catch {
      setLocalMsgs((prev) => [
        ...prev,
        { role: 'assistant', content: 'Sorry, I could not process that request. Please try again.' },
      ]);
      pushToast('Chat request failed', 'error');
    } finally {
      setSending(false);
    }
  };

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <>
      {/* Toggle button */}
      <motion.button
        whileHover={{ scale: 1.05 }}
        whileTap={{ scale: 0.95 }}
        onClick={() => setOpen((o) => !o)}
        className="fixed bottom-6 right-6 z-40 flex h-14 w-14 items-center justify-center rounded-2xl bg-brand-gradient shadow-brand text-white"
        aria-label="Toggle chat"
      >
        {open ? <X className="h-6 w-6" /> : <MessageSquare className="h-6 w-6" />}
      </motion.button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: 20, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.95 }}
            transition={{ type: 'spring', stiffness: 300, damping: 26 }}
            className="fixed bottom-24 right-6 z-40 flex flex-col w-[calc(100vw-3rem)] sm:w-96 h-[32rem] max-h-[70vh] rounded-2xl bg-white shadow-card-hover ring-1 ring-slate-200 overflow-hidden"
          >
            {/* Header */}
            <div className="flex items-center gap-2.5 bg-brand-gradient px-4 py-3 text-white">
              <Sparkles className="h-5 w-5" />
              <div className="flex-1">
                <p className="text-sm font-semibold">Lead Assistant</p>
                <p className="text-xs text-white/70">Ask about this session's companies</p>
              </div>
            </div>

            {/* Messages */}
            <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4 space-y-3 bg-slate-50/40">
              {historyLoading ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="h-5 w-5 text-brand-400 animate-spin" />
                </div>
              ) : messages.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-12 text-center">
                  <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-brand-100 text-brand-400 mb-3">
                    <MessageSquare className="h-6 w-6" />
                  </div>
                  <p className="text-sm font-medium text-slate-600">No messages yet</p>
                  <p className="text-xs text-slate-400 mt-1 max-w-[220px]">
                    Ask me anything about this session's companies, scores, or growth signals…
                  </p>
                </div>
              ) : (
                messages.map((msg, i) => <MessageBubble key={i} msg={msg} />)
              )}
            </div>

            {/* Input */}
            <div className="border-t border-slate-100 p-3 bg-white">
              <div className="flex items-end gap-2">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKey}
                  placeholder="Type a message…"
                  rows={1}
                  className="flex-1 resize-none rounded-xl border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:border-brand-400 focus:outline-none focus:ring-2 focus:ring-brand-100 max-h-24"
                />
                <button
                  onClick={handleSend}
                  disabled={!input.trim() || sending}
                  className="flex h-9 w-9 items-center justify-center rounded-xl bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-40 transition-colors shrink-0"
                  aria-label="Send"
                >
                  {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                </button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}

function MessageBubble({ msg }: { msg: ChatMessage & { sources?: ChatSource[]; tool_used?: string } }) {
  const isUser = msg.role === 'user';
  const sources = safeArr(msg.sources);
  const toolLabel = msg.tool_used
    ? msg.tool_used === 'sql_lookup' ? 'via database'
    : msg.tool_used === 'semantic_search' ? 'via web research'
    : `via ${msg.tool_used}`
    : null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}
    >
      <div className={`max-w-[85%] ${isUser ? '' : 'w-full'}`}>
        <div
          className={`rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed ${
            isUser
              ? 'bg-brand-600 text-white rounded-br-md'
              : 'bg-white text-slate-700 ring-1 ring-slate-100 rounded-bl-md'
          }`}
        >
                    {isUser ? (
            msg.content || '—'
          ) : (
            <div className="prose prose-sm max-w-none prose-slate
                            prose-p:my-1 prose-ol:my-1 prose-ul:my-1 prose-li:my-0.5
                            prose-a:text-sky-600">
              <ReactMarkdown>{msg.content || '—'}</ReactMarkdown>
            </div>
          )}
        </div>

        {/* Tool tag */}
        {toolLabel && (
          <div className="flex items-center gap-1 mt-1.5 text-xs text-slate-400">
            <Wrench className="h-3 w-3" /> {toolLabel}
          </div>
        )}

        {/* Sources */}
        {sources.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2">
            {sources.map((s, i) => (
              <a
                key={i}
                href={s.website_url?.startsWith('http') ? s.website_url : `https://${s.website_url}`}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 rounded-lg bg-sky-50 px-2 py-0.5 text-xs text-sky-700 ring-1 ring-sky-200 hover:bg-sky-100 transition-colors"
              >
                {s.company_name}
                <ExternalLink className="h-3 w-3" />
              </a>
            ))}
          </div>
        )}
      </div>
    </motion.div>
  );
}
