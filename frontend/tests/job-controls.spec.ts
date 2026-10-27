import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'test-token'); localStorage.setItem('username', 'admin');
  });
  await page.route('https://fonts.googleapis.com/**', (route) => route.abort());
  await page.route('https://fonts.gstatic.com/**', (route) => route.abort());
});

async function mockJob(page: import('@playwright/test').Page, initial = 'running') {
  const job = { job_id: 'job-pause', status: initial, file_name: 'leads.csv', file_type: 'csv',
    total_rows: 10, processed_rows: 3, failed_rows: 0, progress_pct: 30,
    reused_rows: 0, research_reused_rows: 0, processing_mode: 'reuse',
    icp_prompt: 'Software companies', created_at: '2026-10-27T10:00:00Z', completed_at: null };
  let pauses = 0;
  await page.route('http://127.0.0.1:5179/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api', '');
    if (path.endsWith('/pause')) { pauses++; job.status = 'paused'; return route.fulfill({ json: job }); }
    if (path.endsWith('/resume') || path.endsWith('/icp')) { job.status = 'running'; return route.fulfill({ json: job }); }
    if (path === '/upload') return route.fulfill({ json: { ...job, status: 'importing' } });
    if (path.endsWith('/status')) return route.fulfill({ json: job });
    if (path === '/jobs') return route.fulfill({ json: [job] });
    if (path === '/dashboard/stats') return route.fulfill({ json: {
      total_jobs: 1, completed_jobs: 0, active_jobs: job.status === 'running' ? 1 : 0,
      failed_jobs: 0, total_leads: 10, high_priority: 0, medium_priority: 0,
      low_priority: 0, avg_final_score: 0,
    } });
    if (path.endsWith('/results')) return route.fulfill({ json: { items: [], total: 0, has_more: false } });
    if (path === '/health') return route.fulfill({ json: { status: 'ok' } });
    return route.fulfill({ json: {} });
  });
  return { job, pauses: () => pauses };
}

test('job page pauses, persists across reload, and resumes saved progress', async ({ page }, info) => {
  const state = await mockJob(page);
  await page.goto('/jobs/job-pause');
  await page.getByRole('button', { name: 'Pause job', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Resume job', exact: true })).toBeVisible();
  await expect(page.getByText('Paused — saved progress')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Delete', exact: true })).toBeDisabled();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Resume job', exact: true })).toBeVisible();
  await expect(page.getByText('Processed: 3', { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('paused-job.png'), fullPage: true });
  await page.getByRole('button', { name: 'Resume job', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Pause job', exact: true })).toBeVisible();
  expect(state.pauses()).toBe(1);
  expect(state.job.processed_rows).toBe(3);
});

test('dashboard controls pause and resume without navigating away', async ({ page }) => {
  await mockJob(page);
  await page.goto('/dashboard');
  await page.getByRole('button', { name: 'Pause job', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Resume job', exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.getByRole('button', { name: 'Resume job', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Pause job', exact: true })).toBeVisible();
});

test('failed pause keeps the running job and offers a retry', async ({ page }) => {
  await mockJob(page);
  await page.goto('/jobs/job-pause');
  await page.route('**/jobs/job-pause/pause', (route) => route.fulfill({ status: 409, json: { detail: 'Job finished elsewhere. Refresh status.' } }), { times: 1 });
  await page.getByRole('button', { name: 'Pause job', exact: true }).click();
  await expect(page.getByText('Job finished elsewhere. Refresh status.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Pause job', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Pause job', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Resume job', exact: true })).toBeVisible();
});

test('completed job has no pause or resume control', async ({ page }) => {
  await mockJob(page, 'completed');
  await page.goto('/jobs/job-pause');
  await expect(page.getByRole('heading', { name: 'leads.csv', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /^(Pause|Resume) job$/ })).toHaveCount(0);
});

test('upload processing view can pause and resume', async ({ page }) => {
  await mockJob(page, 'queued');
  await page.goto('/upload');
  await page.locator('input[type=file]').setInputFiles({ name: 'leads.csv', mimeType: 'text/csv', buffer: Buffer.from('Company Name\nAcme\n') });
  await page.getByRole('button', { name: 'Upload & Continue', exact: true }).click();
  await page.getByLabel('Describe your Ideal Customer Profile').fill('Software companies');
  await page.getByRole('button', { name: 'Start Processing', exact: true }).click();
  await page.getByRole('button', { name: 'Pause job', exact: true }).click();
  await expect(page.getByText('Job paused', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Resume job', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Pause job', exact: true })).toBeVisible();
});
