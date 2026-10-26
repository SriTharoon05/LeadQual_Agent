import { useState, useCallback, useRef, useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  UploadCloud, FileSpreadsheet, CheckCircle2, XCircle, Sparkles,
  Loader2, ArrowRight, FileUp, Target, Zap,
} from 'lucide-react';
import { TopBar } from '@/components/TopBar';
import { ProcessingModeChoice } from '@/components/ProcessingModeChoice';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Textarea, Label } from '@/components/ui/Input';
import { useToast } from '@/components/ui/Toast';
import { uploadFile, startIcp, getJobStatus } from '@/api/endpoints';
import { ALLOWED_EXT, isValidFile, fmtBytes } from '@/lib/format';
import type { JobStatus, ProcessingMode } from '@/types';
import { newRequestId } from '@/lib/leadCache';
import { isAxiosError } from 'axios';

// 'importing' step added: parse + column-map + bulk-insert now run in a
// background Celery task, so the UI polls import status after uploading.
type Step = 'select' | 'uploading' | 'importing' | 'icp' | 'processing' | 'done';

const STAGES = [
  { label: 'Uploading file',                    threshold: 0 },
  { label: 'Extracting & mapping columns',      threshold: 10 },
  { label: 'Deduplicating rows',                threshold: 20 },
  { label: 'Validating leads (ICP / Web / Contact)', threshold: 35 },
  { label: 'Enriching & scoring',               threshold: 70 },
  { label: 'Completed',                         threshold: 100 },
];

function uploadRequestId(file: File, forceNew = false): { id: string; recovered: boolean } {
  const key = `lead-upload:request:${localStorage.getItem('username') ?? ''}`;
  const identity = JSON.stringify([file.name, file.size, file.lastModified]);
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
    if (!forceNew && saved?.identity === identity && typeof saved.id === 'string'
        && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(saved.id)) return { id: saved.id, recovered: true };
  } catch { /* Use a fresh ID if draft metadata was unavailable/corrupt. */ }
  const id = newRequestId();
  try { sessionStorage.setItem(key, JSON.stringify({ identity, id })); } catch { /* Ref retains retries. */ }
  return { id, recovered: false };
}

function forgetUploadRequest(jobId: string) {
  try {
    const key = `lead-upload:request:${localStorage.getItem('username') ?? ''}`;
    if (JSON.parse(sessionStorage.getItem(key) || 'null')?.id === jobId) sessionStorage.removeItem(key);
  } catch { /* A disabled store cannot prevent processing. */ }
}

export function UploadPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const prefill = location.state as { icpPrompt?: unknown; processingMode?: unknown } | null;
  const { toast } = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [step, setStep] = useState<Step>('select');
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [icpPrompt, setIcpPrompt] = useState(typeof prefill?.icpPrompt === 'string' ? prefill.icpPrompt.slice(0, 8000) : '');
  const [processingMode, setProcessingMode] = useState<ProcessingMode>(prefill?.processingMode === 'fresh' ? 'fresh' : 'reuse');
  const [jobId, setJobId] = useState<string | null>(null);
  // Import counts now arrive from polling (not from the instant upload response).
  const [importStats, setImportStats] = useState<{ total_rows: number; duplicates_removed: number } | null>(null);
  const uploadBusy = useRef(false);
  const icpBusy = useRef(false);
  const uploadRequest = useRef<{ file: File; id: string } | null>(null);

  const handleFileSelect = useCallback((f: File) => {
    if (!isValidFile(f.name)) {
      toast(`Invalid file type. Allowed: ${ALLOWED_EXT.join(', ')}`, 'error');
      return;
    }
    setFile(f);
  }, [toast]);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files?.[0];
    if (f) handleFileSelect(f);
  }, [handleFileSelect]);

  const handleUpload = async () => {
    if (!file || uploadBusy.current) return;
    uploadBusy.current = true;
    const previousUpload = uploadRequest.current;
    const candidate = previousUpload && previousUpload.file === file ? { id: previousUpload.id, recovered: false } : uploadRequestId(file);
    let requestId = candidate.id;
    let uploadStarted = false;
    uploadRequest.current = { file, id: requestId };
    setStep('uploading');
    try {
      if (candidate.recovered) {
        try {
          const existing = await getJobStatus(requestId);
          if (['running', 'completed', 'failed', 'import_failed'].includes(existing.status)) {
            requestId = uploadRequestId(file, true).id;
            uploadRequest.current = { file, id: requestId };
          }
        } catch (cause) {
          if (!isAxiosError(cause) || cause.response?.status !== 404) throw cause;
        }
      }
      uploadStarted = true;
      const res = await uploadFile(file, requestId);
      setJobId(res.job_id);
      // Upload returns instantly with status "importing" — poll import status next.
      setStep('importing');
    } catch (cause) {
      if (!uploadStarted) {
        uploadRequest.current = null;
        toast('Could not check the earlier upload. Retry when the connection recovers; no new upload was sent.', 'error');
        setStep('select');
        return;
      }
      const response = isAxiosError(cause) ? cause.response : undefined;
      const code = response?.status;
      if (code != null && code >= 400 && code < 500 && code !== 408 && code !== 429) {
        // An explicit rejection did not accept this transfer. Recovery is for
        // ambiguous timeouts/server failures, not a proxy's size-limit response.
        const detail = response?.data?.detail;
        toast(typeof detail === 'string' ? detail : code === 413
          ? 'This file exceeds the server upload limit. Try a smaller file or ask your administrator to increase the limit.'
          : 'The upload was rejected. Check the file and your sign-in status, then try again.', 'error');
        setStep('select');
        return;
      }
      try {
        const current = await getJobStatus(requestId);
        if (!current.upload_pending && current.status !== 'import_failed') {
          setJobId(current.job_id); setStep('importing');
          toast('The upload was accepted. Its current status has been recovered.', 'info');
          return;
        }
      } catch { /* The same request ID is retained for a safe upload retry. */ }
      const detail = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
      toast(typeof detail === 'string' ? detail : 'Could not confirm the upload. Retry the same file safely; it will reuse the same upload request.', 'error');
      setStep('select');
    } finally { uploadBusy.current = false; }
  };

  const handleIcp = async () => {
    if (!icpPrompt.trim() || !jobId || icpBusy.current) return;
    icpBusy.current = true;
    setStep('processing');
    try {
      await startIcp(jobId, icpPrompt.trim(), processingMode);
      forgetUploadRequest(jobId);
      toast('Processing started', 'success');
    } catch {
      try {
        const current = await getJobStatus(jobId);
        if (['running', 'completed', 'failed'].includes(current.status)
            && current.icp_prompt === icpPrompt.trim() && (current.processing_mode ?? 'reuse') === processingMode) {
          forgetUploadRequest(jobId);
          toast('The processing request was accepted. Loading its current status.', 'info');
          return;
        }
      } catch { /* Keep the imported job; the dashboard can recover its status. */ }
      toast('Could not confirm processing. Check this upload on the dashboard before trying again.', 'error');
      setStep('icp');
    } finally { icpBusy.current = false; }
  };

  return (
    <div className="min-h-screen bg-sky-gradient">
      <TopBar />
      <main className="mx-auto max-w-3xl px-4 sm:px-6 lg:px-8 py-8">
        <div className="mb-8">
          <h1 className="text-2xl font-bold text-slate-800">Upload & Qualify Leads</h1>
          <p className="text-sm text-slate-500 mt-1">Upload a lead file, define your ICP, and let AI qualify them</p>
          {prefill?.processingMode === 'fresh' && <p className="mt-3 rounded-xl border border-sky-200 bg-sky-50 p-3 text-sm text-sky-900">Upload the original file again to create a new run. Your ICP is prefilled and Fresh AI research is selected; you can switch to reuse before starting. Previous results stay available.</p>}
        </div>

        {/* Stepper indicator */}
        <div className="flex items-center gap-2 mb-8">
          {[
            { id: 'select', label: 'Select File', icon: FileUp },
            { id: 'icp', label: 'Define ICP', icon: Target },
            { id: 'processing', label: 'Processing', icon: Zap },
            { id: 'done', label: 'Results', icon: CheckCircle2 },
          ].map((s, i) => {
            const stepOrder = ['select', 'uploading', 'importing', 'icp', 'processing', 'done'];
            const currentIdx = stepOrder.indexOf(step);
            const sIdx = stepOrder.indexOf(s.id);
            const active = sIdx <= currentIdx;
            const Icon = s.icon;
            return (
              <div key={s.id} className="flex items-center flex-1 last:flex-none">
                <div className={`flex items-center gap-2 ${active ? '' : 'opacity-40'}`}>
                  <div className={`flex h-8 w-8 items-center justify-center rounded-full ${active ? 'bg-brand-600 text-white' : 'bg-slate-200 text-slate-400'}`}>
                    <Icon className="h-4 w-4" />
                  </div>
                  <span className={`text-xs font-medium hidden sm:block ${active ? 'text-slate-700' : 'text-slate-400'}`}>{s.label}</span>
                </div>
                {i < 3 && <div className={`flex-1 h-0.5 mx-2 ${sIdx < currentIdx ? 'bg-brand-400' : 'bg-slate-200'}`} />}
              </div>
            );
          })}
        </div>

        <AnimatePresence mode="wait">
          {/* Step: Select file */}
          {step === 'select' && (
            <motion.div key="select" initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }}>
              <Card className="p-6">
                <div
                  onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                  onDragLeave={() => setDragging(false)}
                  onDrop={onDrop}
                  onClick={() => fileInputRef.current?.click()}
                  className={`flex flex-col items-center justify-center border-2 border-dashed rounded-2xl py-16 cursor-pointer transition-all ${
                    dragging ? 'border-brand-400 bg-brand-50' : 'border-slate-200 hover:border-brand-300 hover:bg-brand-50/30'
                  }`}
                >
                  <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-brand-100 text-brand-500 mb-4">
                    <UploadCloud className="h-7 w-7" />
                  </div>
                  <p className="text-sm font-medium text-slate-700">Drag & drop your file here</p>
                  <p className="text-xs text-slate-400 mt-1">or click to browse</p>
                  <p className="text-xs text-slate-400 mt-3">Supported: {ALLOWED_EXT.join(', ')}</p>
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept={ALLOWED_EXT.join(',')}
                    className="hidden"
                    onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFileSelect(f); }}
                  />
                </div>

                {file && (
                  <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="mt-4 flex items-center gap-3 rounded-xl bg-brand-50 ring-1 ring-brand-100 px-4 py-3">
                    <FileSpreadsheet className="h-8 w-8 text-brand-500 shrink-0" />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-slate-800 truncate">{file.name}</p>
                      <p className="text-xs text-slate-500">{fmtBytes(file.size)}</p>
                    </div>
                    <button onClick={() => setFile(null)} className="text-slate-400 hover:text-rose-500 transition-colors">
                      <XCircle className="h-5 w-5" />
                    </button>
                  </motion.div>
                )}

                <div className="mt-6 flex justify-end">
                  <Button onClick={handleUpload} disabled={!file} icon={<ArrowRight className="h-4 w-4" />}>
                    Upload & Continue
                  </Button>
                </div>
              </Card>
            </motion.div>
          )}

          {/* Step: Uploading */}
          {step === 'uploading' && (
            <motion.div key="uploading" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
              <Card className="p-12 flex flex-col items-center">
                <Loader2 className="h-10 w-10 text-brand-500 animate-spin mb-4" />
                <p className="text-sm font-medium text-slate-700">Uploading file…</p>
                <p className="text-xs text-slate-400 mt-1">{file?.name}</p>
              </Card>
            </motion.div>
          )}

          {/* Step: Importing (parse + column-map + bulk-insert run in the background) */}
          {step === 'importing' && jobId && (
            <motion.div key="importing" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
              <ImportingView
                jobId={jobId}
                onReady={(stats) => { setImportStats(stats); setStep('icp'); }}
                onFailed={(msg) => { toast(msg || 'Import failed', 'error'); setStep('select'); }}
                onAlreadyStarted={() => navigate(`/jobs/${jobId}`)}
              />
            </motion.div>
          )}

          {/* Step: ICP */}
          {step === 'icp' && jobId && (
            <motion.div key="icp" initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }}>
              <Card className="p-6 space-y-5">
                {/* Import summary (counts come from the background import) */}
                <div className="rounded-xl bg-emerald-50 ring-1 ring-emerald-100 px-4 py-3 flex items-center gap-3">
                  <CheckCircle2 className="h-5 w-5 text-emerald-500 shrink-0" />
                  <div className="flex-1 text-sm">
                    <span className="font-medium text-slate-700">{importStats?.total_rows ?? 0}</span> leads imported
                    {(importStats?.duplicates_removed ?? 0) > 0 && (
                      <span className="text-slate-500"> · {importStats?.duplicates_removed} duplicates removed</span>
                    )}
                  </div>
                </div>

                <div>
                  <Label htmlFor="icp"><span className="inline-flex items-center gap-1.5"><Sparkles className="h-4 w-4 text-brand-500" />Describe your Ideal Customer Profile</span></Label>
                  <Textarea
                    id="icp"
                    rows={5}
                    value={icpPrompt}
                    onChange={(e) => setIcpPrompt(e.target.value)}
                    placeholder="e.g. B2B SaaS companies in North America with 50-500 employees, using Salesforce, actively hiring for sales roles, and showing growth signals like recent funding rounds…"
                  />
                  <p className="text-xs text-slate-400 mt-1.5">The more specific your ICP, the better the qualification accuracy.</p>
                </div>

                <ProcessingModeChoice value={processingMode} onChange={setProcessingMode} disabled={icpBusy.current} />
                <div className="flex justify-between">
                  <Button variant="ghost" onClick={() => setStep('select')}>Back</Button>
                  <Button onClick={handleIcp} disabled={!icpPrompt.trim()} icon={<Zap className="h-4 w-4" />}>
                    Start Processing
                  </Button>
                </div>
              </Card>
            </motion.div>
          )}

          {/* Step: Processing */}
          {step === 'processing' && jobId && (
            <motion.div key="processing" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
              <ProcessingView
                jobId={jobId}
                onComplete={() => navigate(`/jobs/${jobId}`)}
                onDashboard={() => navigate('/dashboard')}
              />
            </motion.div>
          )}
        </AnimatePresence>
      </main>
    </div>
  );
}

// ── Importing view: polls job status until the background import finishes ─────
function ImportingView({ jobId, onReady, onFailed, onAlreadyStarted }: {
  jobId: string;
  onReady: (stats: { total_rows: number; duplicates_removed: number }) => void;
  onFailed: (msg: string) => void;
  onAlreadyStarted: () => void;
}) {
  const notified = useRef(false);   // fire the callback only once

  const { data: status, isError, refetch } = useQuery({
    queryKey: ['job-status', jobId],
    queryFn: ({ signal }) => getJobStatus(jobId, signal),
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      // stop polling once the import is done (either way)
      return s && s !== 'importing' ? false : 2500;
    },
    placeholderData: (prev) => prev,
    refetchOnWindowFocus: false,
  });

const s = status?.status;

useEffect(() => {
  if (!notified.current && s === 'queued') {
    notified.current = true;
    onReady({
      total_rows: status?.total_rows ?? 0,
      duplicates_removed: status?.duplicates_removed ?? 0,
    });
  }
  if (!notified.current && s === 'import_failed') {
    notified.current = true;
    onFailed(status?.import_error ?? 'Import failed');
  }
  if (!notified.current && s && ['running', 'completed', 'failed'].includes(s)) {
    notified.current = true;
    onAlreadyStarted();
  }
}, [s, status?.total_rows, status?.duplicates_removed, status?.import_error, onReady, onFailed, onAlreadyStarted]);

  return (
    <Card className="p-12 flex flex-col items-center">
      <Loader2 className="h-10 w-10 text-brand-500 animate-spin mb-4" />
      <p className="text-sm font-medium text-slate-700">Extracting columns &amp; inserting rows…</p>
      <p className="text-xs text-slate-400 mt-1">Analyzing your file — this can take a moment for large files.</p>
      {isError && <p role="alert" className="mt-4 text-sm text-rose-700">Could not check import progress. Your upload may still be importing. <button className="underline" onClick={() => void refetch()}>Retry status</button></p>}
    </Card>
  );
}

function ProcessingView({ jobId, onComplete, onDashboard }: {
  jobId: string;
  onComplete: () => void;
  onDashboard: () => void;
}) {
  const navigated = useRef(false);

  const { data: status, refetch, isError } = useQuery({
    queryKey: ['job-status', jobId],
    queryFn: ({ signal }) => getJobStatus(jobId, signal),
    refetchInterval: (query) => ['completed', 'failed', 'import_failed'].includes(query.state.data?.status || '') ? false : 3000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    staleTime: 0,
  });

  useEffect(() => {
    if (status?.status === 'completed' && !navigated.current) {
      const t = setTimeout(() => { navigated.current = true; onComplete(); }, 800);
      return () => clearTimeout(t);
    }
  }, [status?.status, onComplete]);

  const jobStatus: JobStatus | undefined = status?.status;
  const pct = Math.max(0, Math.min(100, status?.progress_pct ?? 0));
  const activeStageIdx = STAGES.reduce((acc, s, i) => (pct >= s.threshold ? i : acc), 0);

  // Show loading until we have real data
  if (!status) {
    return (
      <Card className="p-12 flex flex-col items-center">
        <Loader2 className="h-10 w-10 text-brand-500 animate-spin mb-4" />
        <p className="text-sm font-medium text-slate-700">Connecting to processing pipeline…</p>
        {isError && <p role="alert" className="mt-3 text-sm text-rose-700">Could not load progress. <button className="underline" onClick={() => void refetch()}>Retry</button></p>}
      </Card>
    );
  }

  return (
    <Card className="p-8">
      {isError && <p role="alert" className="mb-4 text-sm text-rose-700">Progress could not be refreshed; the last saved status is shown. <button className="underline" onClick={() => void refetch()}>Retry status</button></p>}
      <div className="flex items-center gap-3 mb-6">
        {jobStatus === 'failed' ? (
          <XCircle className="h-8 w-8 text-rose-500" />
        ) : jobStatus === 'completed' ? (
          <CheckCircle2 className="h-8 w-8 text-emerald-500" />
        ) : (
          <div className="flex h-8 w-8 items-center justify-center rounded-full bg-brand-100">
            <Loader2 className="h-5 w-5 text-brand-500 animate-spin" />
          </div>
        )}
        <div>
          <p className="text-base font-semibold text-slate-800">
            {jobStatus === 'failed'
              ? 'Processing failed'
              : jobStatus === 'completed'
              ? 'Processing complete!'
              : 'Processing your leads…'}
          </p>
          <p className="text-xs text-slate-500">
            {status?.processed_rows ?? 0} / {status?.total_rows ?? 0} rows processed
          </p>
        </div>
      </div>

      <div className="mb-6">
        <div className="flex justify-between text-xs text-slate-500 mb-1.5">
          <span>Progress</span>
          <span className="font-semibold tabular-nums">{pct.toFixed(0)}%</span>
        </div>
        <div className="h-3 rounded-full bg-slate-100 overflow-hidden">
          <motion.div
            className={`h-full rounded-full ${
              jobStatus === 'failed'
                ? 'bg-rose-400'
                : 'bg-gradient-to-r from-brand-400 to-brand-600'
            }`}
            animate={{ width: `${pct}%` }}
            transition={{ duration: 0.5 }}
          />
        </div>
      </div>

      <div className="space-y-3">
        {STAGES.map((stage, i) => {
          const done = i < activeStageIdx || jobStatus === 'completed';
          const active = i === activeStageIdx && jobStatus !== 'completed' && jobStatus !== 'failed';
          const failed = jobStatus === 'failed' && i === activeStageIdx;
          return (
            <div key={stage.label} className="flex items-center gap-3">
              <div className={`flex h-7 w-7 items-center justify-center rounded-full shrink-0 transition-colors ${
                done   ? 'bg-emerald-100 text-emerald-600' :
                failed ? 'bg-rose-100 text-rose-600' :
                active ? 'bg-brand-100 text-brand-600' :
                         'bg-slate-100 text-slate-300'
              }`}>
                {done   ? <CheckCircle2 className="h-4 w-4" /> :
                 failed ? <XCircle className="h-4 w-4" /> :
                 active ? <Loader2 className="h-4 w-4 animate-spin" /> :
                          <span className="text-xs font-bold">{i + 1}</span>}
              </div>
              <span className={`text-sm ${
                done   ? 'text-slate-600' :
                active ? 'text-slate-800 font-medium' :
                         'text-slate-400'
              }`}>
                {stage.label}
              </span>
              {active && <span className="h-1.5 w-1.5 rounded-full bg-brand-400 animate-pulse" />}
            </div>
          );
        })}
      </div>

      {jobStatus !== 'failed' && jobStatus !== 'completed' && (
        <div className="mt-6 rounded-xl bg-sky-50 ring-1 ring-sky-100 p-4 sm:flex sm:items-center sm:justify-between sm:gap-4">
          <div>
            <p className="text-sm font-medium text-slate-700">No need to stay on this screen</p>
            <p className="text-xs text-slate-500 mt-1">Processing is asynchronous. The dashboard lobby keeps showing live progress while you do other work.</p>
          </div>
          <Button variant="outline" className="mt-3 sm:mt-0 shrink-0" onClick={onDashboard}>
            Go to Dashboard &amp; Wait
          </Button>
        </div>
      )}

      {jobStatus === 'failed' && (
        <div className="mt-6 rounded-xl bg-rose-50 ring-1 ring-rose-100 px-4 py-3 text-sm text-rose-600">
          This job could not finish successfully. Open it from the dashboard to inspect saved results, or contact an administrator before processing the file again.
        </div>
      )}

      {jobStatus === 'completed' && (
        <div className="mt-6 flex justify-end">
          <Button onClick={onComplete} icon={<ArrowRight className="h-4 w-4" />}>
            View Results
          </Button>
        </div>
      )}
    </Card>
  );
}

