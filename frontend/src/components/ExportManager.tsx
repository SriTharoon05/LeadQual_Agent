import { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ChevronDown, ChevronUp, Download, Loader2, RefreshCw, X } from 'lucide-react';
import { useLocation } from 'react-router-dom';
import { exportDownloadUrl, getExportStatus, prepareExport } from '@/api/endpoints';
import type { ExportStatus } from '@/api/endpoints';
import { fmtBytes } from '@/lib/format';

type Format = 'excel' | 'csv';
type Item = Partial<ExportStatus> & { job_id: string; format: Format; busy: boolean; problem?: string; link?: string; linkAt?: number };
interface ExportActions {
  start: (jobId: string, format: Format) => void;
  isBusy: (jobId: string, format: Format) => boolean;
}
const Context = createContext<ExportActions | null>(null);
const keyOf = (jobId: string, format: Format) => `${jobId}:${format}`;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STATES = new Set(['queued', 'running', 'ready', 'failed']);
const storageKey = (owner: string) => `lead-exports:v2:${encodeURIComponent(owner)}`;
type Session = { owner: string; token: string };

function session(): Session | null {
  try {
    const owner = localStorage.getItem('username');
    const token = localStorage.getItem('token');
    return owner?.trim() && token?.trim() ? { owner, token } : null;
  } catch { return null; }
}

function nonnegative(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function optionalTime(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : undefined;
}

function validSnapshot(value: unknown): string | undefined {
  return typeof value === 'string' && value.length <= 100 && Number.isFinite(Date.parse(value)) ? value : undefined;
}

function restored(owner: string | null): Item[] {
  if (!owner) return [];
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey(owner)) ?? 'null');
    // Older storage had no owner envelope and could be written across logins.
    // A fresh request recovers the same server-side file without trusting it.
    if (!saved || saved.version !== 2 || saved.owner !== owner || !Array.isArray(saved.items)) return [];
    const unique = new Map<string, Item>();
    for (const item of saved.items) {
      if (!item || typeof item.job_id !== 'string' || !UUID.test(item.job_id)
          || typeof item.export_id !== 'string' || !UUID.test(item.export_id)
          || !['excel', 'csv'].includes(item.format) || !STATES.has(item.status)
          || !nonnegative(item.rows_written) || !nonnegative(item.total_rows)) continue;
      const entry: Item = {
        job_id: item.job_id.toLowerCase(), export_id: item.export_id.toLowerCase(), format: item.format,
        status: item.status, rows_written: item.rows_written, total_rows: item.total_rows, busy: false,
        created_at: optionalTime(item.created_at), ready_at: optionalTime(item.ready_at),
        snapshot_at: validSnapshot(item.snapshot_at), size_bytes: nonnegative(item.size_bytes) ? item.size_bytes : undefined,
        phase: item.status === 'ready' ? 'Ready to download again' : item.status === 'failed' ? 'Previous export failed' : 'Resuming export',
        problem: item.status === 'failed' ? 'The previous export did not finish. Retry to create a new file.' : undefined,
      };
      unique.set(keyOf(entry.job_id, entry.format), entry);
    }
    return [...unique.values()].slice(-10);
  } catch { return []; }
}

function checkedStatus(value: ExportStatus, jobId: string, format: Format): ExportStatus {
  if (!value || typeof value !== 'object' || typeof value.export_id !== 'string' || !UUID.test(value.export_id)
      || typeof value.job_id !== 'string' || value.job_id.toLowerCase() !== jobId.toLowerCase()
      || value.format !== format || !STATES.has(value.status)
      || !nonnegative(value.rows_written) || !nonnegative(value.total_rows)
      || !optionalTime(value.created_at)
      || (value.status === 'ready' && (value.rows_written !== value.total_rows
          || !nonnegative(value.size_bytes) || value.size_bytes === 0
          || typeof value.download_path !== 'string' || value.download_path.length > 4096))) {
    throw new Error('The export service returned an invalid result. Retry to recover the file.');
  }
  return { ...value, job_id: jobId.toLowerCase(), export_id: value.export_id.toLowerCase(),
    phase: typeof value.phase === 'string' ? value.phase.slice(0, 300) : 'Preparing export',
    error: typeof value.error === 'string' ? value.error.slice(0, 1000) : null,
    snapshot_at: validSnapshot(value.snapshot_at), size_bytes: nonnegative(value.size_bytes) ? value.size_bytes : undefined,
    filename: `job_${jobId.toLowerCase()}.${format === 'excel' ? 'xlsx' : 'csv'}` };
}

function message(error: unknown): string {
  const err = error as { response?: { data?: { detail?: unknown } }; message?: string };
  const detail = err.response?.data?.detail;
  return typeof detail === 'string' ? detail : 'Could not reach the export service. Retry to resume your export.';
}

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('Aborted', 'AbortError')); return; }
    const abort = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); };
    const timer = window.setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, ms);
    signal.addEventListener('abort', abort, { once: true });
  });
}

export function ExportProvider({ children }: { children: ReactNode }) {
  // Subscribe to route changes so setAuth() followed by SPA navigation restores
  // the newly signed-in owner's state instead of reusing the login-page state.
  useLocation();
  const [, authChanged] = useState(0);
  useEffect(() => {
    const sync = () => authChanged((version) => version + 1);
    const storage = (event: StorageEvent) => { if (!event.key || event.key === 'username' || event.key === 'token') sync(); };
    const visible = () => { if (!document.hidden) sync(); };
    window.addEventListener('storage', storage);
    window.addEventListener('focus', sync);
    document.addEventListener('visibilitychange', visible);
    return () => {
      window.removeEventListener('storage', storage);
      window.removeEventListener('focus', sync);
      document.removeEventListener('visibilitychange', visible);
    };
  }, []);
  const auth = session();
  // React's reconciliation key is not a DOM attribute. Persistence below holds
  // export IDs/metadata only, never authentication or signed download URLs.
  return <SessionExports key={auth ? JSON.stringify([auth.owner, auth.token]) : 'signed-out'}
    owner={auth?.owner ?? null} token={auth?.token ?? null}>{children}</SessionExports>;
}

function SessionExports({ children, owner, token }: { children: ReactNode; owner: string | null; token: string | null }) {
  const [items, setItems] = useState<Item[]>(() => restored(owner));
  const [collapsed, setCollapsed] = useState(true);
  const current = useRef(items);
  const active = useRef(new Map<string, AbortController>());
  const downloadFrames = useRef(new Map<HTMLIFrameElement, number>());
  const location = useLocation();
  const bodyId = useId();

  const ownsSession = useCallback(() => {
    const auth = session();
    return Boolean(owner && token && auth?.owner === owner && auth.token === token);
  }, [owner, token]);

  const change = useCallback((key: string, values: Partial<Item>) => {
    if (!ownsSession()) return;
    current.current = current.current.map((item) => keyOf(item.job_id, item.format) === key ? { ...item, ...values } : item);
    setItems(current.current);
  }, [ownsSession]);

  const beginDownload = useCallback((url: string) => {
    if (!ownsSession()) return;
    const frame = document.createElement('iframe');
    frame.hidden = true;
    frame.title = 'Lead export download';
    frame.referrerPolicy = 'no-referrer';
    frame.src = url;
    document.body.appendChild(frame);
    const timer = window.setTimeout(() => { frame.remove(); downloadFrames.current.delete(frame); }, 30 * 60_000);
    downloadFrames.current.set(frame, timer);
  }, [ownsSession]);

  const run = useCallback(async (jobId: string, format: Format, resumeId?: string, downloadOnly = false, automatic = false) => {
    if (!ownsSession() || !UUID.test(jobId) || (resumeId && !UUID.test(resumeId))) return;
    jobId = jobId.toLowerCase();
    if (!automatic) setCollapsed(false);
    const key = keyOf(jobId, format);
    if (active.current.has(key)) return;
    const controller = new AbortController();
    const { signal } = controller;
    active.current.set(key, controller);
    const item: Item = { job_id: jobId, format, status: 'queued', phase: 'Requesting export', busy: true };
    const existing = current.current.some((value) => keyOf(value.job_id, value.format) === key);
    if (existing) change(key, resumeId
      ? { busy: true, phase: 'Checking export', problem: undefined, link: undefined }
      : { ...item, export_id: undefined, rows_written: 0, total_rows: 0, ready_at: undefined,
          problem: undefined, link: undefined, error: null, snapshot_at: undefined });
    else {
      const retained = current.current.filter((value) => value.busy);
      const room = Math.max(0, 9 - retained.length);
      const finished = room ? current.current.filter((value) => !value.busy).slice(-room) : [];
      current.current = [...retained, ...finished, item]; setItems(current.current);
    }
    if (active.current.size > 3) {
      active.current.delete(key);
      change(key, { busy: false, problem: 'Three exports are already preparing. Retry when one finishes.' });
      return;
    }
    try {
      let status: ExportStatus | undefined;
      let failures = 0;
      const started = Date.now();
      let exportId = resumeId;
      while (!signal.aborted && ownsSession()) {
        try {
          const response = exportId ? await getExportStatus(jobId, exportId, signal) : await prepareExport(jobId, format, signal);
          if (signal.aborted || !ownsSession()) return;
          status = checkedStatus(response, jobId, format);
          exportId = status.export_id;
          failures = 0;
        } catch (error) {
          if (signal.aborted || !ownsSession()) return;
          if (error instanceof Error && !(error as { isAxiosError?: boolean }).isAxiosError) throw error;
          const httpStatus = (error as { response?: { status?: number } }).response?.status;
          if (httpStatus === 404 || httpStatus === 410) change(key, { status: 'failed', export_id: undefined });
          if ((httpStatus && httpStatus < 500 && httpStatus !== 429) || ++failures > 5) throw error;
          change(key, { phase: 'Connection interrupted — reconnecting' });
          await pause(Math.min(1000 * 2 ** failures, 10_000), signal);
          continue;
        }
        if (signal.aborted || !ownsSession()) return;
        change(key, { ...status, busy: true });
        if (status.status === 'failed') throw new Error(status.error || 'Export failed. Please retry.');
        if (status.status === 'ready') {
          if (!status.download_path) throw new Error('The download link is missing. Please retry.');
          const path = status.download_path.match(/^\/jobs\/([^/]+)\/exports\/([^/]+)\/file\?ticket=/i);
          if (path?.[1].toLowerCase() !== jobId || path?.[2].toLowerCase() !== status.export_id) throw new Error('The download link does not match this export. Please retry.');
          const url = exportDownloadUrl(status.download_path);
          change(key, { busy: false, link: url, linkAt: Date.now(), phase: automatic
            ? 'Ready to download' : 'Ready — use Download file if your browser does not start it' });
          // Reloads restore progress, never trigger an unsolicited old download.
          if (!automatic) beginDownload(url);
          break;
        }
        if (downloadOnly) throw new Error('This file is no longer ready. Please create a new export.');
        if (Date.now() - started > 125 * 60_000) throw new Error('The export is still waiting. Check the worker, then retry to resume.');
        await pause(status.status === 'queued' ? 2000 : 1500, signal);
      }
    } catch (error) {
      if (!signal.aborted && ownsSession()) {
        const known = error instanceof Error && !(error as { isAxiosError?: boolean }).isAxiosError;
        change(key, { busy: false, problem: known ? error.message : message(error) });
      }
    } finally {
      if (active.current.get(key) === controller) {
        active.current.delete(key);
        if (!signal.aborted && ownsSession()) change(key, { busy: false });
      }
    }
  }, [change, ownsSession, beginDownload]);

  useEffect(() => {
    // The owner is captured by this keyed session. Never read a different
    // username here after an in-flight logout/login transition.
    if (!owner) return;
    try {
      sessionStorage.setItem(storageKey(owner), JSON.stringify({ version: 2, owner, items: items.filter((item) => item.export_id).map((item) => ({
        job_id: item.job_id, format: item.format, export_id: item.export_id, status: item.status,
        created_at: item.created_at, ready_at: item.ready_at, snapshot_at: item.snapshot_at, size_bytes: item.size_bytes,
        rows_written: item.rows_written, total_rows: item.total_rows,
        phase: item.status === 'ready' ? 'Ready to download again' : item.phase, busy: false,
      })) }));
    } catch { /* Downloads still work if browser storage is disabled/full. */ }
  }, [items, owner]);

  useEffect(() => {
    for (const item of current.current) {
      if (item.export_id && (item.status === 'queued' || item.status === 'running')) void run(item.job_id, item.format, item.export_id, false, true);
    }
    return () => {
      active.current.forEach((controller) => controller.abort()); active.current.clear();
      downloadFrames.current.forEach((timer, frame) => { clearTimeout(timer); frame.remove(); });
      downloadFrames.current.clear();
    };
  }, [run]);

  const dismiss = (item: Item) => {
    if (!ownsSession()) return;
    const key = keyOf(item.job_id, item.format);
    active.current.get(key)?.abort();
    active.current.delete(key);
    current.current = current.current.filter((value) => keyOf(value.job_id, value.format) !== key);
    setItems(current.current);
  };

  // Row progress updates the tray only. Dashboard/table subscribers need a new
  // context value only when an export enters or leaves the busy set.
  const busySignature = items.filter((item) => item.busy).map((item) => keyOf(item.job_id, item.format)).sort().join('\n');
  const busyKeys = useMemo(() => new Set(busySignature.split('\n').filter(Boolean)), [busySignature]);
  const actions = useMemo<ExportActions>(() => ({
    start: (jobId, format) => { void run(jobId, format); },
    isBusy: (jobId, format) => busyKeys.has(keyOf(jobId.toLowerCase(), format)),
  }), [run, busyKeys]);

  return (
    <Context.Provider value={actions}>
      {children}
      {owner && location.pathname !== '/login' && items.length > 0 && (
        <aside aria-label="File exports" className={`fixed bottom-4 left-4 z-30 ${collapsed ? 'w-[min(20rem,calc(100vw-6.5rem))]' : 'w-[min(24rem,calc(100vw-2rem))]'} max-h-[50vh] overflow-y-auto rounded-xl border border-slate-200 bg-white shadow-xl`}>
          <button type="button" aria-expanded={!collapsed} aria-controls={bodyId} onClick={() => setCollapsed((value) => !value)} className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm font-semibold text-slate-700">
            <Download className="h-4 w-4 shrink-0" /><span className="flex-1 truncate">File exports{items.some((item) => item.busy) ? ` · ${items.filter((item) => item.busy).length} preparing` : ` · ${items.length}`}</span>
            {collapsed ? <ChevronUp className="h-4 w-4 shrink-0" /> : <ChevronDown className="h-4 w-4 shrink-0" />}
          </button>
          <div id={bodyId} hidden={collapsed} className="border-t border-slate-100">
          {items.map((item) => {
            const label = item.format === 'excel' ? 'Excel' : 'CSV';
            const percent = item.total_rows ? Math.min(95, Math.round((item.rows_written ?? 0) / item.total_rows * 95)) : 0;
            return (
              <div key={keyOf(item.job_id, item.format)} className="p-3 border-b last:border-0 border-slate-100">
                <div className="flex items-center gap-2">
                  {item.busy ? <Loader2 className="h-4 w-4 animate-spin text-brand-600" /> : <Download className="h-4 w-4 text-emerald-600" />}
                  <span className="text-sm font-medium">{label} · Job {item.job_id.slice(0, 8)}</span>
                  <button type="button" onClick={() => dismiss(item)} aria-label={`Dismiss ${label} export`} className="ml-auto rounded p-1 text-slate-400 hover:text-slate-700"><X className="h-4 w-4" /></button>
                </div>
                <p role="status" className={`mt-1 text-xs ${item.problem ? 'text-rose-600' : 'text-slate-500'}`}>{item.problem || item.phase}</p>
                {item.snapshot_at && !item.busy && <p className="mt-1 text-xs text-slate-400">Snapshot: {new Date(item.snapshot_at).toLocaleString()}</p>}
                {item.busy && <>
                  <p className="text-xs text-slate-500 mt-1">{(item.rows_written ?? 0).toLocaleString()} / {(item.total_rows ?? 0).toLocaleString()} rows · You can keep working</p>
                  <div role="progressbar" aria-label={`${label} export preparation`} aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100} className="mt-2 h-1.5 rounded bg-slate-100 overflow-hidden"><div className="h-full bg-brand-500" style={{ width: `${percent}%` }} /></div>
                </>}
                {!item.busy && !item.problem && item.status === 'ready' && <div className="mt-2 flex gap-3 items-center text-xs font-semibold text-brand-700">
                  {item.link && <a href={item.link} target="_blank" rel="noopener noreferrer" download={item.filename ?? undefined} className="underline" onClick={(event) => {
                    if (!ownsSession()) { event.preventDefault(); return; }
                    if (!item.linkAt || Date.now() - item.linkAt > 14 * 60_000) {
                      event.preventDefault(); void run(item.job_id, item.format, item.export_id, true);
                    }
                  }}>Download file{item.size_bytes ? ` · ${fmtBytes(item.size_bytes)}` : ''}</a>}
                  <button type="button" onClick={() => { void run(item.job_id, item.format, item.export_id, true); }} className="underline">Download again</button>
                </div>}
                {item.problem && <button type="button" onClick={() => { void run(item.job_id, item.format, item.status === 'failed' ? undefined : item.export_id); }} className="mt-2 inline-flex items-center gap-1 text-xs font-semibold text-brand-700"><RefreshCw className="h-3 w-3" />Retry export</button>}
              </div>
            );
          })}
          </div>
        </aside>
      )}
    </Context.Provider>
  );
}

export function useExports(): ExportActions {
  const context = useContext(Context);
  if (!context) throw new Error('ExportProvider is missing');
  return context;
}
