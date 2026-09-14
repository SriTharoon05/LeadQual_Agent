import {
  Building2, User, Mail, Award, Lightbulb, CheckSquare, Link2, Cpu, Send,
} from 'lucide-react';
import {
  Radar, RadarChart, PolarGrid, PolarAngleAxis, PolarRadiusAxis,
  ResponsiveContainer,
} from 'recharts';
import { Modal } from '@/components/ui/Modal';
import { Badge, PriorityBadge } from '@/components/ui/Badge';
import { dash, num, safeArr, numVal } from '@/lib/format';
import type { LeadResult } from '@/types';
import { IcpEvidence, SourceGlobe } from './IcpEvidence';
import { LeadTracking } from './LeadTracking';

export function LeadDetailDrawer({ lead, open, onClose, jobId, onSaved }: {
  jobId: string;
  onSaved: (lead: LeadResult) => void;
  lead: LeadResult | null;
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Modal open={open} onClose={onClose} size="xl" title="Lead Detail">
      {lead && <LeadDetailContent key={lead.lead_id} lead={lead} jobId={jobId} onSaved={onSaved} />}
    </Modal>
  );
}

function LeadDetailContent({ lead, jobId, onSaved }: { lead: LeadResult; jobId: string; onSaved: (lead: LeadResult) => void }) {
  const outreach = parseOutreach(lead.outreach_intelligence);
  const radarData = [
    { metric: 'ICP',      value: numVal(lead.icp_score) },
    { metric: 'Authority',value: numVal(lead.authority_score) },
    { metric: 'Data',     value: numVal(lead.data_score) },
    { metric: 'Growth',   value: numVal(lead.growth_score) },
    { metric: 'Contact',  value: numVal(lead.contact_score) },
  ];
  const hasScores = radarData.some((d) => d.value > 0);

  return (
    <div className="space-y-6">
      {/* Top: company + final score */}
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
        <div className="flex items-start gap-3">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-brand-100 text-brand-600 shrink-0">
            <Building2 className="h-6 w-6" />
          </div>
          <div>
            <h3 className="text-lg font-bold text-slate-800">{dash(lead.company_name)}</h3>
            <p className="text-sm text-slate-500">{dash(lead.job_title)}</p>
            <div className="flex items-center gap-2 mt-2">
              <PriorityBadge priority={lead.priority} />
              {lead.validation_status && <Badge color={lead.validation_status === 'Passed' ? 'success' : 'warning'}>{lead.validation_status}</Badge>}
            </div>
          </div>
        </div>
        <div className="flex flex-col items-center bg-brand-50 rounded-2xl px-6 py-3 ring-1 ring-brand-100">
          <span className="text-xs font-medium text-slate-500">Final Score</span>
          <span className="text-3xl font-bold text-brand-700 mt-1">{num(lead.final_score)}</span>
        </div>
      </div>

      {/* Company block */}
      <Section icon={<Building2 className="h-4 w-4" />} title="Company">
        {lead.company_description ? (
          <p className="text-sm text-slate-600 leading-relaxed">{lead.company_description}</p>
        ) : (
          <Empty text="No company description available" />
        )}
        {lead.technologies_used && (
          <div className="mt-3 flex items-start gap-2">
            <Cpu className="h-4 w-4 text-slate-400 mt-0.5 shrink-0" />
            <div>
              <span className="text-xs font-medium text-slate-500">Technologies</span>
              <p className="text-sm text-slate-700 mt-0.5">{lead.technologies_used}</p>
            </div>
          </div>
        )}
        {/* Source URLs */}
        <div className="mt-3">
          <span className="text-xs font-medium text-slate-500 flex items-center gap-1.5 mb-1.5"><Link2 className="h-3.5 w-3.5" />Sources</span>
          {safeArr(lead.source_urls).length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {lead.source_urls.map((url, i) => (
                <SourceGlobe key={i} url={url} />
              ))}
            </div>
          ) : (
            <p className="text-xs text-slate-400">No sources</p>
          )}
        </div>
      </Section>

      {/* Contact block */}
      <Section icon={<User className="h-4 w-4" />} title="Contact">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
          <InfoRow label="Name" value={dash(lead.contact_name)} />
          <InfoRow label="Job Title" value={dash(lead.job_title)} />
          <InfoRow label="Email" value={lead.email_address ?? '—'} icon={Mail} link={lead.email_address ? `mailto:${lead.email_address}` : undefined} />
        </div>
      </Section>

      {/* Scoring block */}
      <Section icon={<Award className="h-4 w-4" />} title="Scoring Breakdown">
        {hasScores ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 items-center">
            <div className="h-[200px]">
              <ResponsiveContainer width="100%" height="100%">
                <RadarChart data={radarData}>
                  <PolarGrid stroke="#e2e8f0" />
                  <PolarAngleAxis dataKey="metric" tick={{ fontSize: 11, fill: '#64748b' }} />
                  <PolarRadiusAxis domain={[0, 100]} tick={{ fontSize: 9, fill: '#94a3b8' }} />
                  <Radar dataKey="value" stroke="#7c3aed" fill="#7c3aed" fillOpacity={0.25} strokeWidth={2} />
                </RadarChart>
              </ResponsiveContainer>
            </div>
            <div className="space-y-2">
              <ScoreRow label="ICP Score"       value={lead.icp_score} />
              <ScoreRow label="Authority Score" value={lead.authority_score} />
              <ScoreRow label="Data Score"      value={lead.data_score} />
              <ScoreRow label="Growth Score"    value={lead.growth_score} />
              <ScoreRow label="Contact Score"   value={lead.contact_score} />
            </div>
          </div>
        ) : (
          <Empty text="No scoring data available" />
        )}
      </Section>

      <Section icon={<CheckSquare className="h-4 w-4" />} title="Lead tracking">
        <LeadTracking lead={lead} jobId={jobId} onSaved={onSaved} notes />
      </Section>

      <Section icon={<Lightbulb className="h-4 w-4" />} title="AI Reasoning">
        {lead.reuse_kind && <p className="mb-3 rounded-lg bg-sky-50 p-2 text-xs text-sky-800">{lead.reuse_kind === 'full' ? 'Saved result reused from an earlier upload.' : 'Saved research reused; qualification recalculated for this upload.'}</p>}
        <IcpEvidence lead={lead} />
      </Section>

      {/* Growth signals */}
      {lead.growth_signals && (
        <Section icon={<CheckSquare className="h-4 w-4" />} title="Growth Signals">
          <p className="text-sm text-slate-600 leading-relaxed">{lead.growth_signals}</p>
        </Section>
      )}

      {/* Actionable, evidence-grounded next best outreach */}
      {lead.outreach_intelligence && (
        <Section icon={<Send className="h-4 w-4" />} title="Outreach Intelligence">
          {outreach ? (
            <div className="space-y-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-slate-500">Best channel</span>
                <Badge color="brand">{outreach.recommended_channel || 'Not specified'}</Badge>
              </div>
              <InfoBlock label="Best angle" value={outreach.best_angle} />
              <InfoBlock label="Suggested opener" value={outreach.opening_message} quote />
              {outreach.talking_points.length > 0 && (
                <div>
                  <p className="text-xs font-medium text-slate-500 mb-1">Talking points</p>
                  <ul className="list-disc pl-5 space-y-1 text-slate-600">
                    {outreach.talking_points.map((point, index) => <li key={index}>{point}</li>)}
                  </ul>
                </div>
              )}
              <InfoBlock label="Objection watchout" value={outreach.objection_watchout} />
            </div>
          ) : (
            <p className="text-sm text-slate-600 whitespace-pre-wrap">{lead.outreach_intelligence}</p>
          )}
        </Section>
      )}
    </div>
  );
}

type Outreach = {
  recommended_channel: string;
  best_angle: string;
  opening_message: string;
  talking_points: string[];
  objection_watchout: string;
};

function parseOutreach(value: string | null): Outreach | null {
  if (!value) return null;
  try {
    const data = JSON.parse(value) as Partial<Outreach>;
    return {
      recommended_channel: String(data.recommended_channel ?? ''),
      best_angle: String(data.best_angle ?? ''),
      opening_message: String(data.opening_message ?? ''),
      talking_points: Array.isArray(data.talking_points) ? data.talking_points.map(String) : [],
      objection_watchout: String(data.objection_watchout ?? ''),
    };
  } catch {
    return null;
  }
}

function InfoBlock({ label, value, quote = false }: { label: string; value: string; quote?: boolean }) {
  if (!value) return null;
  return (
    <div>
      <p className="text-xs font-medium text-slate-500 mb-1">{label}</p>
      <p className={`leading-relaxed ${quote ? 'rounded-lg bg-white p-3 italic text-slate-700 ring-1 ring-slate-200' : 'text-slate-600'}`}>{value}</p>
    </div>
  );
}

function Section({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl bg-slate-50/60 ring-1 ring-slate-100 p-4">
      <h4 className="flex items-center gap-2 text-sm font-semibold text-slate-700 mb-3">
        <span className="text-brand-500">{icon}</span>
        {title}
      </h4>
      {children}
    </div>
  );
}

function InfoRow({ label, value, icon: Icon, link }: { label: string; value: string; icon?: typeof Mail; link?: string }) {
  const content = link ? (
    <a href={link} className="text-brand-600 hover:underline">{value}</a>
  ) : (
    <span className="text-slate-700">{value}</span>
  );
  return (
    <div className="flex items-center gap-2">
      {Icon && <Icon className="h-4 w-4 text-slate-400 shrink-0" />}
      <div>
        <span className="text-xs text-slate-400">{label}: </span>
        {content}
      </div>
    </div>
  );
}

function ScoreRow({ label, value }: { label: string; value: number | null | undefined }) {
  const v = numVal(value);
  return (
    <div className="flex items-center justify-between">
      <span className="text-sm text-slate-600">{label}</span>
      <div className="flex items-center gap-2">
        <div className="w-24 h-1.5 rounded-full bg-slate-200 overflow-hidden">
          <div className="h-full bg-brand-400 rounded-full" style={{ width: `${v}%` }} />
        </div>
        <span className="text-sm font-semibold text-slate-700 tabular-nums w-10 text-right">{num(value)}</span>
      </div>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <p className="text-sm text-slate-400">{text}</p>;
}
