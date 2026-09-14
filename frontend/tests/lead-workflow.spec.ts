import { expect, test } from '@playwright/test';
import type { LeadResult } from '../src/types';

const fixture: LeadResult = {
  lead_id: 'lead-1', contact_name: 'Alice Smith', company_name: 'Acme',
  job_title: 'Director', email_address: 'alice@example.com', final_score: 72,
  priority: 'Medium', reasoning: 'Company size matches; revenue remains unclear.',
  growth_signals: null, outreach_intelligence: null,
  icp_score: 60, authority_score: 80, data_score: 70, growth_score: 50, contact_score: 90,
  validation_status: 'Passed', source_urls: [], company_description: 'Business software company.',
  technologies_used: null, reviewed: false, tracking_notes: '', tracking_version: 0,
  reuse_kind: 'full', reused_from_lead_id: 'original-lead', icp_input: 'US software companies with 200–1,000 employees',
  evidence_checked_at: '2026-09-01T10:00:00Z', employment_status: 'current',
  icp_proofs: [
    { field: 'employee_count', label: 'Company size', value: '450 employees', status: 'confirmed',
      explanation: 'Reported in the company annual report.', requirement: '200–1,000 employees',
      verdict: 'match', reasoning: '450 is within the requested range.', checked_at: '2026-09-01T10:00:00Z',
      sources: [{ url: 'https://example.com/report', title: 'Company annual report', excerpt: 'We employ 450 people.' }] },
    { field: 'annual_revenue', label: 'Annual revenue', value: 'Unclear — reliable evidence unavailable', status: 'unclear',
      explanation: 'No reliable supporting evidence available.', requirement: 'Not specified', verdict: 'not_specified',
      reasoning: '', checked_at: '2026-09-01T10:00:00Z', sources: [] },
    { field: 'employment', label: 'Current employment', value: 'current', status: 'confirmed',
      explanation: 'Listed on the current team page.', requirement: 'Currently employed at the uploaded company',
      verdict: 'match', reasoning: 'Current employment confirmed.', checked_at: '2026-09-01T10:00:00Z',
      sources: [{ url: 'https://example.com/team', title: 'Company team', excerpt: 'Alice Smith, Director' }] },
  ],
};

test.beforeEach(async ({ page }) => {
  let lead = structuredClone(fixture);
  await page.addInitScript(() => { localStorage.setItem('token', 'test-token'); localStorage.setItem('username', 'admin'); });
  await page.route('https://fonts.googleapis.com/**', (route) => route.abort());
  await page.route('https://fonts.gstatic.com/**', (route) => route.abort());
  await page.route('http://127.0.0.1:5179/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api', '');
    const job = { job_id: 'job-1', status: 'completed', file_name: 'Reuploaded leads.xlsx',
      total_rows: 1, processed_rows: 1, failed_rows: 0, reused_rows: 1, research_reused_rows: 0,
      progress_pct: 100, created_at: '2026-09-01T10:00:00Z', completed_at: '2026-09-01T10:01:00Z' };
    if (path.endsWith('/tracking')) {
      const body = route.request().postDataJSON();
      if (body.version !== lead.tracking_version) return route.fulfill({ status: 409, json: { detail: 'Stale edit' } });
      lead = { ...lead, reviewed: body.reviewed ?? lead.reviewed, tracking_notes: body.notes ?? lead.tracking_notes,
        tracking_version: lead.tracking_version + 1 };
      return route.fulfill({ json: lead });
    }
    if (path.endsWith('/results/lead-1')) return route.fulfill({ json: lead });
    if (path.endsWith('/results')) return route.fulfill({ json: { items: [lead], total: 1, limit: 50, offset: 0, returned: 1, has_more: false } });
    if (path.endsWith('/status')) return route.fulfill({ json: job });
    if (path === '/jobs') return route.fulfill({ json: [job] });
    if (path.endsWith('/chat/history')) return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
  await page.goto('/jobs/job-1', { waitUntil: 'domcontentloaded' });
});

test('review checkbox, notes and saved evidence survive page reload', async ({ page }, info) => {
  const check = page.getByRole('checkbox', { name: 'Reviewed: Alice Smith' });
  await expect(check).toBeVisible();
  await check.check();
  await expect(check).toBeChecked();
  await expect(page.getByRole('heading', { name: 'Lead Detail', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Add notes', exact: true }).click();
  await page.getByLabel('Tracking notes · shared across uploads').fill('Call Friday after the product demo.');
  await page.getByRole('button', { name: 'Save notes', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Save notes', exact: true })).toBeDisabled();
  await page.getByText('ICP validation proofs', { exact: true }).scrollIntoViewIfNeeded();
  await expect(page.getByText('450 employees', { exact: true })).toBeVisible();
  await expect(page.getByText('Currently employed', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: /Open evidence: Company annual report/ })).toHaveAttribute('href', 'https://example.com/report');
  await expect(page.getByText('https://example.com/report', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('icp-evidence.png'), fullPage: true });
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('checkbox', { name: 'Reviewed: Alice Smith' })).toBeChecked();
  const noteButton = page.getByRole('button', { name: /Call Friday|View notes/ });
  await noteButton.click();
  await expect(page.getByLabel('Tracking notes · shared across uploads')).toHaveValue('Call Friday after the product demo.');
});

test('notes conflict retains the user draft and allows deliberate retry', async ({ page }) => {
  await page.getByRole('button', { name: 'Add notes', exact: true }).click();
  const notes = page.getByLabel('Tracking notes · shared across uploads');
  await notes.fill('My unsaved draft');
  await page.route('**/tracking', async (route) => route.fulfill({ status: 409, json: { detail: 'Updated elsewhere' } }), { times: 1 });
  await page.getByRole('button', { name: 'Save notes', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('your draft is retained');
  await expect(notes).toHaveValue('My unsaved draft');
  await page.getByRole('button', { name: 'Save notes', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Save notes', exact: true })).toBeDisabled();
});
