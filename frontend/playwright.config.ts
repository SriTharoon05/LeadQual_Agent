import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  workers: 2,
  use: { baseURL: 'http://127.0.0.1:5179', trace: 'retain-on-failure' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 7'] } },
  ],
  webServer: {
    command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 5179 --strictPort',
    url: 'http://127.0.0.1:5179',
    env: { VITE_API_BASE_URL: 'http://127.0.0.1:5179/api' },
    reuseExistingServer: false,
  },
});
