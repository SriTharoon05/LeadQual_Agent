import { Fragment, useState, useRef, useEffect, useLayoutEffect, useMemo, useId } from 'react';
import type { KeyboardEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import {
  MessageSquare, X, Send, Loader2, Sparkles, ExternalLink, RefreshCw,
  AlertCircle, ArrowDown, Check, Pencil,
} from 'lucide-react';
import { isAxiosError } from 'axios';
import { getChatHistory, sendChat } from '@/api/endpoints';
import { safeArr } from '@/lib/format';
import type { ChatMessage, ChatRequest, ChatSelection } from '@/types';
import { ChatMarkdown } from '@/components/ChatMarkdown';

const MAX_MESSAGE_LENGTH = 4_000;
const HISTORY_PAGE_SIZE = 100;
const MAX_LOCAL_MESSAGES = 100;
const EMPTY_MESSAGES: ChatMessage[] = [];
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface PendingTurn {
  request: ChatRequest;
  userMessage: ChatMessage;
  failure?: SendFailure;
}

interface SendFailure {
  message: string;
  canEdit: boolean;
}

function newId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const value = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
}

function sessionValue(key: string): string | null {
  try { return sessionStorage.getItem(key); } catch { return null; }
}

function saveSessionValue(key: string, value: string | null): void {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    // Restricted storage must not prevent chatting. State still lasts until navigation.
  }
}

function conversationForJob(jobId: string, owner: string): string {
  const key = `lead-chat:conversation:${encodeURIComponent(owner)}:${jobId}`;
  const stored = sessionValue(key);
  if (stored && UUID_PATTERN.test(stored)) {
    return stored;
  }
  const id = newId();
  saveSessionValue(key, id);
  return id;
}

function readPendingTurn(key: string, conversationId: string): PendingTurn | null {
  try {
    const value = sessionValue(key);
    if (!value) return null;
    const turn: PendingTurn = JSON.parse(value);
    if (turn.request?.conversation_id !== conversationId || !UUID_PATTERN.test(turn.request.request_id)
        || typeof turn.request.message !== 'string' || !turn.request.message.trim()
        || turn.request.message.length > MAX_MESSAGE_LENGTH || turn.userMessage?.role !== 'user'
        || turn.userMessage.id !== `local-${turn.request.request_id}`
        || turn.userMessage.request_id !== turn.request.request_id || turn.userMessage.content !== turn.request.message
        || !Number.isFinite(Date.parse(turn.userMessage.created_at))) return null;
    if (turn.request.selection && (!['company', 'person'].includes(turn.request.selection.kind)
        || typeof turn.request.selection.value !== 'string' || !turn.request.selection.value.trim()
        || turn.request.selection.value.length > 500)) return null;
    if (turn.failure && (typeof turn.failure.message !== 'string' || typeof turn.failure.canEdit !== 'boolean')) turn.failure = undefined;
    return turn;
  } catch { return null; }
}

function savedMessage(value: ChatMessage | undefined, role: 'user' | 'assistant', requestId: string): value is ChatMessage {
  return Boolean(value && typeof value.id === 'string' && !value.id.startsWith('local-')
    && value.role === role && value.request_id === requestId && typeof value.content === 'string'
    && value.content.trim() && Number.isInteger(value.sequence) && value.sequence > 0
    && Number.isFinite(Date.parse(value.created_at)));
}

function messageKey(message: ChatMessage): string {
  return message.request_id ? `${message.request_id}:${message.role}` : message.id;
}

/** The server's sequence is authoritative; optimistic copies share the same turn key. */
function mergeMessages(history: ChatMessage[], local: ChatMessage[]): ChatMessage[] {
  const byTurn = new Map<string, ChatMessage>();
  for (const message of [...safeArr(history), ...local]) {
    const key = messageKey(message);
    const existing = byTurn.get(key);
    // A server history refresh must retain the persisted version of an optimistic user bubble.
    if (existing && message.id.startsWith('local-') && !existing.id.startsWith('local-')) continue;
    byTurn.set(key, message);
  }
  return Array.from(byTurn.values()).sort((a, b) => {
    const aLocal = a.id.startsWith('local-');
    const bLocal = b.id.startsWith('local-');
    // A question sent while history is unavailable cannot know the next server
    // sequence. Keep it after saved history until its persisted turn arrives.
    if (aLocal !== bLocal) return aLocal ? 1 : -1;
    if (!aLocal) {
      const sequence = a.sequence - b.sequence;
      if (Number.isFinite(sequence) && sequence !== 0) return sequence;
    }
    const date = Date.parse(a.created_at) - Date.parse(b.created_at);
    if (Number.isFinite(date) && date !== 0) return date;
    if (a.request_id && a.request_id === b.request_id && a.role !== b.role) return a.role === 'user' ? -1 : 1;
    return a.id.localeCompare(b.id);
  });
}

function requestError(error: unknown): SendFailure {
  if (isAxiosError(error)) {
    if (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT') {
      return { message: 'The reply is taking longer than expected. Retry to recover this reply without sending your question twice.', canEdit: false };
    }
    if (!error.response) return { message: 'The connection was interrupted. Check your connection and retry this message.', canEdit: false };
    if (error.response.status === 429) return { message: 'The assistant is busy. Please wait a moment, then retry this message.', canEdit: false };
    if (error.response.status === 409) {
      const detail = error.response.data?.detail;
      const processing = typeof detail === 'string' && /still|progress|processing|prepared|busy/i.test(detail);
      return processing
        ? { message: 'This reply is still being prepared. Please wait a moment, then retry.', canEdit: false }
        : { message: 'That selection or request is no longer valid. Edit your question and ask again.', canEdit: true };
    }
    if (error.response.status === 404) return { message: 'This session is no longer available. Refresh the page to check its status.', canEdit: true };
    if (error.response.status === 400 || error.response.status === 422) {
      return { message: 'The assistant could not accept this message. Edit your question and ask again.', canEdit: true };
    }
    if (error.response.status === 503) return { message: 'The assistant is temporarily unavailable. Please retry in a moment.', canEdit: false };
  }
  return { message: 'The reply could not be completed. Retry this message to continue the conversation.', canEdit: false };
}

/** A keyed session prevents an old request or history response from entering a different job. */
export function ChatWidget({ jobId }: { jobId: string }) {
  const owner = localStorage.getItem('username') ?? '';
  return <ChatSession key={`${owner}:${jobId}`} jobId={jobId} owner={owner} />;
}

function ChatSession({ jobId, owner }: { jobId: string; owner: string }) {
  const [open, setOpen] = useState(false);
  const [conversationId] = useState(() => conversationForJob(jobId, owner));
  const pendingKey = `lead-chat:pending:${encodeURIComponent(owner)}:${jobId}:${conversationId}`;
  const [restoredTurn] = useState(() => readPendingTurn(pendingKey, conversationId));
  const [pendingTurn, setPendingTurn] = useState<PendingTurn | null>(restoredTurn);
  const [localMsgs, setLocalMsgs] = useState<ChatMessage[]>(restoredTurn ? [restoredTurn.userMessage] : []);
  const [earlierMessages, setEarlierMessages] = useState<ChatMessage[]>([]);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [earlierError, setEarlierError] = useState<string | null>(null);
  const [earlierTarget, setEarlierTarget] = useState<number | null>(null);
  const [noEarlierMessages, setNoEarlierMessages] = useState(false);
  const [unavailableGaps, setUnavailableGaps] = useState<number[]>([]);
  const [sendError, setSendError] = useState<SendFailure | null>(restoredTurn
    ? restoredTurn.failure ?? { message: 'The previous reply was interrupted. Retry to recover it and continue.', canEdit: false }
    : null);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [hasNewMessages, setHasNewMessages] = useState(false);
  const sendingRef = useRef(false);
  const mountedRef = useRef(true);
  const requestController = useRef<AbortController | null>(null);
  const earlierController = useRef<AbortController | null>(null);
  const earlierScroll = useRef<{ height: number; top: number; anchorKey?: string; anchorTop?: number } | null>(null);
  const loadingEarlierRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const nearBottomRef = useRef(true);
  const latestVisibleKey = useRef<string | null>(null);
  const panelId = useId();
  const qc = useQueryClient();
  const historyKey = useMemo(() => ['chat-history', jobId, conversationId], [jobId, conversationId]);

  const {
    data: history = EMPTY_MESSAGES, isPending: historyLoading, isError: historyError,
    isFetching: historyFetching, refetch: reloadHistory,
  } = useQuery({
    queryKey: historyKey,
    queryFn: ({ signal }) => getChatHistory(jobId, conversationId, signal),
    enabled: open,
    refetchOnWindowFocus: false,
    retry: 1,
    staleTime: 30_000,
  });

  const messages = useMemo(() => mergeMessages([...earlierMessages, ...history], localMsgs), [earlierMessages, history, localMsgs]);
  const latestAssistant = [...messages].reverse().find((message) => message.role === 'assistant');
  const pendingHasReply = Boolean(pendingTurn && messages.some((message) =>
    message.request_id === pendingTurn.request.request_id && message.role === 'assistant'));
  const blockedByFailure = Boolean(pendingTurn && sendError && !pendingHasReply);
  const clarification = latestAssistant?.metadata?.clarification;
  const lastMessage = messages[messages.length - 1];
  const firstSavedMessage = messages.find((message) => !message.id.startsWith('local-'));
  const hasEarlierMessages = Boolean(firstSavedMessage && firstSavedMessage.sequence > 1 && !noEarlierMessages);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestController.current?.abort();
      earlierController.current?.abort();
    };
  }, []);

  // History can recover a reply completed on the server after a client timeout or refresh.
  useEffect(() => {
    if (!pendingHasReply) return;
    // A history refresh can finish before the original HTTP transfer recovers.
    // Release the input immediately when its saved reply is already visible.
    requestController.current?.abort();
    requestController.current = null;
    sendingRef.current = false;
    setSending(false);
    setPendingTurn(null);
    setSendError(null);
    saveSessionValue(pendingKey, null);
  }, [pendingHasReply, pendingKey]);

  const scrollToLatest = () => {
    nearBottomRef.current = true;
    setHasNewMessages(false);
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  };

  useLayoutEffect(() => {
    if (!open) return;
    const latestKey = messages.length ? messageKey(messages[messages.length - 1]) : null;
    const hasNewTurn = latestVisibleKey.current !== latestKey;
    latestVisibleKey.current = latestKey;
    if (earlierScroll.current && scrollRef.current) {
      const previous = earlierScroll.current;
      earlierScroll.current = null;
      const anchor = [...(contentRef.current?.querySelectorAll<HTMLElement>('[data-chat-message-key]') ?? [])]
        .find((element) => element.dataset.chatMessageKey === previous.anchorKey);
      if (anchor && previous.anchorTop !== undefined) {
        scrollRef.current.scrollTop += anchor.getBoundingClientRect().top - previous.anchorTop;
      } else scrollRef.current.scrollTop = previous.top + scrollRef.current.scrollHeight - previous.height;
      if (hasNewTurn && latestKey) setHasNewMessages(true);
      return;
    }
    if (nearBottomRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
      setHasNewMessages(false);
    } else if (hasNewTurn && messages.length > 0) {
      setHasNewMessages(true);
    }
  }, [messages, sending, open]);

  useEffect(() => {
    if (!open || !contentRef.current) return;
    const observer = new ResizeObserver(() => {
      if (nearBottomRef.current && scrollRef.current) {
        scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
      }
    });
    observer.observe(contentRef.current);
    return () => observer.disconnect();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const frame = requestAnimationFrame(() => inputRef.current?.focus({ preventScroll: true }));
    return () => cancelAnimationFrame(frame);
  }, [open]);

  const loadEarlier = async (beforeSequence?: number) => {
    const oldest = messages.find((message) => !message.id.startsWith('local-'));
    const before = beforeSequence ?? oldest?.sequence;
    if (!before || before <= 1 || loadingEarlierRef.current) return;
    loadingEarlierRef.current = true;
    setLoadingEarlier(true);
    setEarlierError(null);
    setEarlierTarget(before);
    const controller = new AbortController();
    earlierController.current = controller;
    try {
      const page = await getChatHistory(jobId, conversationId, controller.signal, before, HISTORY_PAGE_SIZE);
      if (!mountedRef.current || controller.signal.aborted) return;
      const known = new Set(messages.map(messageKey));
      if (!page.some((message) => !known.has(messageKey(message)))) {
        if (beforeSequence) setUnavailableGaps((previous) => [...previous, beforeSequence]);
        else setNoEarlierMessages(true);
        return;
      }
      if (scrollRef.current) {
        const visibleTop = scrollRef.current.getBoundingClientRect().top;
        const anchor = [...(contentRef.current?.querySelectorAll<HTMLElement>('[data-chat-message-key]') ?? [])]
          .find((element) => element.getBoundingClientRect().bottom >= visibleTop);
        earlierScroll.current = { height: scrollRef.current.scrollHeight, top: scrollRef.current.scrollTop,
          anchorKey: anchor?.dataset.chatMessageKey, anchorTop: anchor?.getBoundingClientRect().top };
        nearBottomRef.current = false;
        setHasNewMessages(false);
      }
      // The latest-page query may move forward while older pages are loading.
      // Retain the current visible history too, so that no intervening turn is lost.
      setEarlierMessages((previous) => mergeMessages(page, mergeMessages(previous, history)));
    } catch {
      if (mountedRef.current && !controller.signal.aborted) setEarlierError('Earlier messages could not load. Retry to keep reading.');
    } finally {
      loadingEarlierRef.current = false;
      earlierController.current = null;
      if (mountedRef.current) setLoadingEarlier(false);
    }
  };

  const handleOpen = () => {
    if (!open) nearBottomRef.current = true;
    setOpen((value) => !value);
  };

  const closeChat = () => {
    setOpen(false);
    toggleRef.current?.focus();
  };

  const performSend = async (turn: PendingTurn) => {
    // Ref lock prevents double Enter/click events before React commits the disabled state.
    if (sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    setSendError(null);
    const currentTurn: PendingTurn = { request: turn.request, userMessage: turn.userMessage };
    setPendingTurn(currentTurn);
    saveSessionValue(pendingKey, JSON.stringify(currentTurn));
    nearBottomRef.current = true;
    setHasNewMessages(false);
    setLocalMsgs((previous) => mergeMessages(previous, [turn.userMessage]));
    const controller = new AbortController();
    requestController.current = controller;

    try {
      const response = await sendChat(jobId, turn.request, controller.signal);
      if (!mountedRef.current || controller.signal.aborted) return;
      if (!savedMessage(response.user_message, 'user', turn.request.request_id)
          || !savedMessage(response.assistant_message, 'assistant', turn.request.request_id)
          || response.assistant_message.sequence !== response.user_message.sequence + 1
          || response.user_message.content !== turn.request.message) {
        throw new Error('Chat response did not contain persisted messages.');
      }
      const assistant: ChatMessage = {
        ...response.assistant_message,
        metadata: {
          ...response.assistant_message.metadata,
          clarification: response.clarification ?? response.assistant_message.metadata?.clarification,
          context: response.context ?? response.assistant_message.metadata?.context,
          degraded: response.degraded ?? response.assistant_message.metadata?.degraded,
        },
      };
      const persisted = [response.user_message, assistant];
      // Keep a session overlay as well as the cache: an older in-flight history read
      // cannot erase a newly acknowledged reply or move its optimistic user message.
      setLocalMsgs((previous) => mergeMessages(previous.filter((message) =>
        message.request_id !== turn.request.request_id), persisted).slice(-MAX_LOCAL_MESSAGES));
      const previousPage = qc.getQueryData<ChatMessage[]>(historyKey) ?? [];
      const updatedPage = mergeMessages(previousPage, mergeMessages(localMsgs.filter((message) => !message.id.startsWith('local-')), persisted));
      const dropped = updatedPage.slice(0, Math.max(0, updatedPage.length - HISTORY_PAGE_SIZE));
      if (dropped.length) setEarlierMessages((previous) => previous.length ? mergeMessages(previous, dropped) : previous);
      qc.setQueryData<ChatMessage[]>(historyKey, updatedPage.slice(-HISTORY_PAGE_SIZE));
      setPendingTurn(null);
      setSendError(null);
      saveSessionValue(pendingKey, null);
    } catch (error) {
      if (mountedRef.current && !controller.signal.aborted) {
        const failure = requestError(error);
        const failedTurn = { ...currentTurn, failure };
        setSendError(failure);
        setPendingTurn(failedTurn);
        saveSessionValue(pendingKey, JSON.stringify(failedTurn));
      }
    } finally {
      if (requestController.current === controller) {
        sendingRef.current = false;
        requestController.current = null;
        if (mountedRef.current) {
          setSending(false);
          if (nearBottomRef.current) inputRef.current?.focus({ preventScroll: true });
        }
      }
    }
  };

  const editFailedMessage = () => {
    if (!pendingTurn || !sendError?.canEdit || sendingRef.current) return;
    setInput(pendingTurn.request.message);
    setLocalMsgs((previous) => previous.filter((message) => message.request_id !== pendingTurn.request.request_id));
    setPendingTurn(null);
    setSendError(null);
    saveSessionValue(pendingKey, null);
    inputRef.current?.focus({ preventScroll: true });
  };

  const handleSend = (selection?: ChatSelection) => {
    const text = selection ? clarification?.original_message?.trim() : input.trim();
    if (!text || text.length > MAX_MESSAGE_LENGTH || sendingRef.current || blockedByFailure || historyLoading) return;
    const requestId = newId();
    const maxSequence = messages.reduce((max, message) =>
      Number.isFinite(message.sequence) ? Math.max(max, message.sequence) : max, 0);
    const turn: PendingTurn = {
      request: { message: text, request_id: requestId, conversation_id: conversationId, ...(selection ? { selection } : {}) },
      userMessage: {
        id: `local-${requestId}`,
        request_id: requestId,
        role: 'user',
        content: text,
        sequence: maxSequence + 1,
        created_at: new Date().toISOString(),
        sources: [],
        tool_used: null,
      },
    };
    if (!selection) setInput('');
    void performSend(turn);
  };

  const handleKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      handleSend();
    }
  };

  return (
    <>
      <motion.button
        ref={toggleRef}
        whileHover={{ scale: 1.05 }}
        whileTap={{ scale: 0.95 }}
        onClick={handleOpen}
        className="fixed bottom-6 right-6 z-40 flex h-14 w-14 items-center justify-center rounded-2xl bg-brand-gradient shadow-brand text-white"
        aria-label={open ? 'Close lead assistant' : 'Open lead assistant'}
        aria-expanded={open}
        aria-controls={panelId}
      >
        {open ? <X className="h-6 w-6" /> : <MessageSquare className="h-6 w-6" />}
      </motion.button>

      <AnimatePresence>
        {open && (
          <motion.section
            id={panelId}
            role="dialog"
            aria-label="Lead assistant"
            initial={{ opacity: 0, y: 20, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.95 }}
            transition={{ type: 'spring', stiffness: 300, damping: 26 }}
            onKeyDown={(event) => { if (event.key === 'Escape') { event.stopPropagation(); closeChat(); } }}
            className="fixed bottom-24 right-3 sm:right-6 z-40 flex flex-col w-[calc(100vw-1.5rem)] sm:w-[38rem] lg:w-[42rem] max-w-[calc(100vw-1.5rem)] h-[40rem] max-h-[calc(100dvh-7rem)] rounded-2xl bg-white shadow-card-hover ring-1 ring-slate-200 overflow-hidden"
          >
            <div className="flex items-center gap-2.5 bg-brand-gradient px-4 py-3 text-white">
              <Sparkles className="h-5 w-5 shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-semibold">Lead Assistant</p>
                <p className="text-xs text-white/80">Search every lead in this session, on any page</p>
              </div>
              <button onClick={closeChat} aria-label="Close assistant" className="rounded-lg p-1 hover:bg-white/15">
                <X className="h-4 w-4" />
              </button>
            </div>

            {historyError && (
              <div role="status" className="flex items-center gap-2 border-b border-amber-100 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                <AlertCircle className="h-4 w-4 shrink-0" />
                <span className="flex-1">Previous messages could not load. You can still ask a question.</span>
                <button onClick={() => { void reloadHistory(); }} disabled={historyFetching}
                  className="rounded px-1.5 py-1 font-semibold hover:bg-amber-100 disabled:opacity-50">
                  {historyFetching ? 'Loading…' : 'Reload'}
                </button>
              </div>
            )}

            <div ref={scrollRef} style={{ overflowAnchor: 'none' }}
              onScroll={() => {
                const element = scrollRef.current;
                if (!element) return;
                nearBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 64;
                if (nearBottomRef.current) setHasNewMessages(false);
              }}
              className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 bg-slate-50/40">
              <div ref={contentRef} role="log" aria-label="Conversation" aria-live="polite" aria-relevant="additions" className="space-y-3">
                {hasEarlierMessages && <div className="text-center">
                  <button type="button" onClick={() => { void loadEarlier(); }} disabled={loadingEarlier}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100 disabled:opacity-50">
                    {loadingEarlier && <Loader2 className="h-3 w-3 animate-spin" />}
                    {loadingEarlier ? 'Loading earlier messages…' : 'Load earlier messages'}
                  </button>
                  {earlierError && earlierTarget === firstSavedMessage?.sequence && <p role="status" className="mt-1 text-xs text-rose-700">{earlierError}</p>}
                </div>}
                {historyLoading && messages.length === 0 ? (
                  <div role="status" className="flex items-center justify-center gap-2 py-8 text-xs text-slate-500">
                    <Loader2 className="h-5 w-5 text-brand-400 animate-spin" /> Loading conversation…
                  </div>
                ) : messages.length === 0 ? (
                  <div className="flex flex-col items-center justify-center py-10 text-center">
                    <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-brand-100 text-brand-400 mb-3">
                      <MessageSquare className="h-6 w-6" />
                    </div>
                    <p className="text-sm font-medium text-slate-600">How can I help with your leads?</p>
                    <p className="text-xs text-slate-500 mt-2 max-w-[270px]">
                      Ask about a company, contact, score, ICP proof, or any row in this session. For example: “Show me row 500”.
                    </p>
                  </div>
                ) : messages.map((message, index) => (
                  <Fragment key={messageKey(message)}>
                  {index > 0 && !message.id.startsWith('local-') && !messages[index - 1].id.startsWith('local-')
                    && message.sequence > messages[index - 1].sequence + 1 && <div className="text-center text-xs text-slate-500">
                      {unavailableGaps.includes(message.sequence) ? 'Messages in this range are no longer available.' : <button type="button"
                        onClick={() => { void loadEarlier(message.sequence); }} disabled={loadingEarlier}
                        className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 hover:bg-slate-100 disabled:opacity-50">
                        {loadingEarlier ? 'Loading messages…' : 'Load messages in this gap'}
                      </button>}
                      {earlierError && earlierTarget === message.sequence && <p role="status" className="mt-1 text-xs text-rose-700">{earlierError}</p>}
                    </div>}
                  <MessageBubble msg={message}
                    onSourcesToggle={() => { nearBottomRef.current = false; }}
                    onChoice={message.id === latestAssistant?.id && lastMessage?.role === 'assistant'
                      ? (selection) => handleSend(selection) : undefined}
                    choicesDisabled={sending || blockedByFailure || historyLoading} />
                  </Fragment>
                ))}

                {sending && !pendingHasReply && <ThinkingIndicator />}
                {blockedByFailure && pendingTurn && (
                  <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-900">
                    <p>{sendError?.message}</p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {sendError?.canEdit ? (
                        <button onClick={editFailedMessage}
                          className="inline-flex items-center gap-1.5 rounded-lg bg-white px-2.5 py-1.5 font-semibold ring-1 ring-rose-200 hover:bg-rose-100">
                          <Pencil className="h-3 w-3" /> Edit / ask again
                        </button>
                      ) : (
                      <button onClick={() => { void performSend(pendingTurn); }} disabled={sending}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-white px-2.5 py-1.5 font-semibold ring-1 ring-rose-200 hover:bg-rose-100 disabled:opacity-50">
                        <RefreshCw className="h-3 w-3" /> Retry message
                      </button>
                      )}
                      <button onClick={() => { void reloadHistory(); }} disabled={historyFetching || sending}
                        className="rounded-lg px-2 py-1.5 underline underline-offset-2 disabled:opacity-50">
                        {historyFetching ? 'Checking…' : 'Check for completed reply'}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>

            {hasNewMessages && (
              <button onClick={scrollToLatest} className="flex items-center justify-center gap-1 border-t border-brand-100 bg-brand-50 px-3 py-1.5 text-xs font-medium text-brand-700">
                <ArrowDown className="h-3 w-3" /> Jump to latest messages
              </button>
            )}

            <div className="border-t border-slate-100 p-3 bg-white">
              <div className="flex items-end gap-2">
                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={handleKey}
                  placeholder={blockedByFailure
                    ? sendError?.canEdit ? 'Edit the previous message to continue…' : 'Retry the previous message to continue…'
                    : 'Ask about any lead…'}
                  aria-label="Message to lead assistant"
                  rows={2}
                  maxLength={MAX_MESSAGE_LENGTH}
                  className="min-w-0 flex-1 resize-none rounded-xl border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:border-brand-400 focus:outline-none focus:ring-2 focus:ring-brand-100 max-h-24"
                />
                <button onClick={() => handleSend()}
                  disabled={!input.trim() || sending || historyLoading || blockedByFailure}
                  className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-40 transition-colors shrink-0"
                  aria-label="Send message">
                  {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                </button>
              </div>
              <p className="mt-1.5 text-[10px] text-slate-400">Enter to send · Shift+Enter for a new line · Answers use saved lead data</p>
            </div>
          </motion.section>
        )}
      </AnimatePresence>
    </>
  );
}

function sourceUrl(value: string): string | null {
  if (typeof value !== 'string' || !value.trim()) return null;
  if (/^[a-z][a-z0-9+.-]*:/i.test(value.trim()) && !/^https?:\/\//i.test(value.trim())) return null;
  const candidate = /^https?:\/\//i.test(value.trim()) ? value.trim() : `https://${value.trim()}`;
  try {
    const url = new URL(candidate);
    return (url.protocol === 'https:' || url.protocol === 'http:') && url.hostname && !url.username && !url.password
      ? url.href : null;
  } catch { return null; }
}

function ThinkingIndicator() {
  const reducedMotion = useReducedMotion();
  return <div role="status" aria-label="Assistant is thinking" className="flex w-fit items-center gap-2.5 rounded-2xl rounded-bl-md bg-white px-4 py-3 text-xs text-slate-500 ring-1 ring-slate-200">
    <span>Thinking</span>
    <span aria-hidden="true" className="flex items-center gap-1">
      {[0, 1, 2].map((index) => <motion.span key={index}
        className="block h-1.5 w-1.5 rounded-full bg-brand-500"
        animate={reducedMotion ? undefined : { y: [0, -3, 0], opacity: [0.4, 1, 0.4] }}
        transition={{ duration: 0.9, delay: index * 0.15, repeat: Infinity, ease: 'easeInOut' }} />)}
    </span>
  </div>;
}

function MessageBubble({ msg, onChoice, choicesDisabled, onSourcesToggle }: {
  msg: ChatMessage;
  onChoice?: (selection: ChatSelection) => void;
  choicesDisabled: boolean;
  onSourcesToggle?: () => void;
}) {
  const [showAllSources, setShowAllSources] = useState(false);
  const isUser = msg.role === 'user';
  const sources = useMemo(() => {
    const unique = new Map<string, { href: string; label: string; title: string }>();
    for (const source of safeArr(msg.sources)) {
      if (!source || typeof source.website_url !== 'string') continue;
      const href = sourceUrl(source.website_url);
      if (!href) continue;
      const url = new URL(href);
      url.hash = '';
      for (const name of [...url.searchParams.keys()]) if (/^utm_|^(?:gclid|fbclid)$/i.test(name)) url.searchParams.delete(name);
      url.searchParams.sort();
      const key = `${url.host.replace(/^www\./, '')}${url.pathname.replace(/\/$/, '')}${url.search}`;
      const label = typeof source.title === 'string' && source.title.trim() ? source.title.slice(0, 160) : url.hostname.replace(/^www\./, '');
      const title = typeof source.excerpt === 'string' && source.excerpt.trim() ? source.excerpt
        : typeof source.company_name === 'string' && source.company_name.trim() ? source.company_name : url.hostname;
      if (!unique.has(key)) unique.set(key, { href, label, title });
    }
    return [...unique.values()];
  }, [msg.sources]);
  const choices = safeArr(msg.metadata?.clarification?.choices).filter((choice) => choice
    && ['company', 'person'].includes(choice.kind) && typeof choice.value === 'string'
    && choice.value.trim() && typeof choice.label === 'string');

  return (
    <div data-chat-message-key={messageKey(msg)} className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div className={`min-w-0 max-w-[92%] ${isUser ? '' : 'w-full'}`}>
        <div className={`rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed break-words ${isUser
          ? 'bg-brand-600 text-white rounded-br-md whitespace-pre-wrap'
          : 'bg-white text-slate-700 ring-1 ring-slate-100 rounded-bl-md'}`}>
          {isUser ? msg.content || '—' : <ChatMarkdown content={msg.content || '—'} />}
        </div>

        {!isUser && msg.metadata?.degraded && (
          <p className="mt-1.5 text-[11px] text-amber-700">AI analysis is temporarily unavailable. This reply uses saved session data.</p>
        )}

        {!isUser && onChoice && choices.length > 0 && (
          <div className="mt-2 flex flex-col gap-1.5" aria-label="Confirm the matching company or contact">
            {choices.slice(0, 8).map((choice) => (
              <button key={`${choice.kind}:${choice.value}`} onClick={() => onChoice({ kind: choice.kind, value: choice.value })}
                disabled={choicesDisabled}
                className="flex items-center gap-2 rounded-lg border border-brand-200 bg-brand-50 px-3 py-2 text-left text-xs text-brand-800 hover:bg-brand-100 disabled:opacity-50">
                <Check className="h-3.5 w-3.5 shrink-0" /> <span className="min-w-0 break-words">{choice.label}</span>
              </button>
            ))}
          </div>
        )}

        {!isUser && sources.length > 0 && <div className="mt-2 space-y-1.5" aria-label="Supporting sources">
          <div className="flex flex-wrap gap-1.5">{(showAllSources ? sources : sources.slice(0, 2)).map((source) => <a key={source.href} href={source.href} target="_blank" rel="noopener noreferrer" title={source.title}
            className="inline-flex max-w-full items-center gap-1 rounded-lg bg-sky-50 px-2 py-1 text-xs text-sky-700 ring-1 ring-sky-200 hover:bg-sky-100">
            <span className="max-w-[220px] truncate">{source.label}</span><ExternalLink className="h-3 w-3 shrink-0" />
          </a>)}</div>
          {sources.length > 2 && <button type="button" aria-expanded={showAllSources} onClick={() => { onSourcesToggle?.(); setShowAllSources((value) => !value); }} className="rounded px-1 py-1 text-xs font-medium text-sky-700 hover:underline">{showAllSources ? 'See less' : `See more sources (${sources.length - 2})`}</button>}
        </div>}
      </div>
    </div>
  );
}
