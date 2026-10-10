import { defineConfig, devices } from '@playwright/test';

const port = Number(process.env.CORSIA_DESIGN_FRONTEND_PORT ?? 5180);

export default defineConfig({
	testDir: './e2e',
	timeout: 30_000,
	fullyParallel: false,
	workers: 1,
	reporter: 'list',
	use: {
		baseURL: `http://127.0.0.1:${port}`,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure',
	},
	projects: [
		{
			name: 'chromium-desktop',
			use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } },
		},
	],
	webServer: {
		// The _design page needs no backend; name an unused loopback port so the
		// proxy never falls back to a real (production) backend.
		command: `CORSIA_BACKEND_URL=${process.env.CORSIA_BACKEND_URL ?? 'http://127.0.0.1:1'} npm run dev -- --host 127.0.0.1 --port ${port}`,
		url: `http://127.0.0.1:${port}/_design`,
		timeout: 120_000,
		reuseExistingServer: false,
		stdout: 'pipe',
		stderr: 'pipe',
	},
});
