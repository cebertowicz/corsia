import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { readBackendUrl, requireBackendUrl } from './src/lib/dev-server/backend-url';

const nodeModulesPath = realpathSync(resolve('node_modules'));

export default defineConfig(() => {
	const backendUrl = readBackendUrl(process.env);
	const proxy = backendUrl
		? {
				'/api': { target: backendUrl, ws: true },
				'/ws': { target: backendUrl.replace(/^http/, 'ws'), ws: true },
			}
		: undefined;

	return {
		plugins: [
			sveltekit(),
			{
				// Dev server only: refuse to start without an explicit backend
				// (no production fallback). Vitest builds a server too; skip it.
				name: 'require-backend-url',
				configureServer() {
					if (!process.env.VITEST) requireBackendUrl(process.env);
				},
			},
		],
		// Vite exposes only VITE_* by default, while the frontend's product events
		// are configured through PUBLIC_POSTHOG_* (docs/observability.md,
		// frontend/.env.example). Without PUBLIC_ here those values never reach
		// import.meta.env, so every frontend emit is a silent no-op in a real
		// build. Covered by src/lib/vite-config.test.ts (#2280).
		envPrefix: ['VITE_', 'PUBLIC_'],
		// Vitest only: resolve Svelte's client entry (mount/flushSync for
		// component tests in jsdom) instead of the SSR entry. No effect on
		// dev/build — VITEST is unset there.
		resolve: process.env.VITEST ? { conditions: ['browser'] } : undefined,
		server: {
			port: 5173,
			fs: {
				allow: [process.cwd(), nodeModulesPath],
			},
			// A fixed-checkout e2e server has no use for HMR: its full-page reloads
			// (SvelteKit rewriting `.svelte-kit/generated` after the first request,
			// the backend writing state under `.qa/`) land mid-test and destroy the page.
			hmr: process.env.CORSIA_E2E_NO_HMR === '1' ? false : undefined,
			proxy,
		},
		test: {
			environment: 'jsdom',
			globals: true,
			include: ['src/**/*.test.ts'],
			setupFiles: ['./vitest.setup.ts'],
			coverage: {
				provider: 'v8',
				include: ['src/lib/**/*.ts'],
				exclude: ['src/lib/**/*.svelte'],
			},
		},
	};
});
