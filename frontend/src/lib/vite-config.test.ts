// @vitest-environment node
import { describe, expect, it, vi } from 'vitest';
import { build } from 'vite';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import viteConfig, { backendOriginForOwnDevRequest } from '../../vite.config';

// The first bundling probe in a worker pays for esbuild/rollup cold start, which
// can pass the 5 s default on a loaded host (same reason playwright-config.test.ts
// raises its timeout).
vi.setConfig({ testTimeout: 30_000 });

/**
 * Which environment variables reach `import.meta.env` is decided by the frontend
 * build config, and `product_events.ts` reads `PUBLIC_POSTHOG_*` from there.
 * Vite's default prefix is `VITE_`, so a config without an explicit `envPrefix`
 * drops every `PUBLIC_*` value: the bundle's env object holds only the built-ins,
 * `emitProductEvent()` finds no API key and no host, and each frontend product
 * event is a silent no-op in production.
 *
 * Tests that stub `import.meta.env` directly cannot see that, so this one bundles
 * a one-line fixture through the shipped config and inspects what Vite actually
 * put in the bundle.
 */
async function bundledImportMetaEnv(): Promise<string> {
	const root = mkdtempSync(join(tmpdir(), 'corsia-env-prefix-'));
	try {
		writeFileSync(
			join(root, '.env'),
			'PUBLIC_TEST_MARKER=public-value\nVITE_TEST_MARKER=vite-value\n',
		);
		writeFileSync(join(root, 'entry.js'), 'export const env = import.meta.env;\n');
		const shipped = viteConfig({ mode: 'production', command: 'build' });
		const bundles = (await build({
			root,
			configFile: false, // this test is about the shipped envPrefix, nothing else
			logLevel: 'silent',
			envPrefix: shipped.envPrefix,
			build: {
				write: false,
				minify: false,
				lib: { entry: join(root, 'entry.js'), formats: ['es'], fileName: 'probe' },
			},
		})) as unknown as { output: { code: string }[] }[];
		return bundles[0].output[0].code;
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
}

describe('frontend env prefix', () => {
	it('exposes PUBLIC_ variables, and still VITE_, on import.meta.env', async () => {
		const code = await bundledImportMetaEnv();
		expect(code).toContain('"PUBLIC_TEST_MARKER": "public-value"');
		expect(code).toContain('"VITE_TEST_MARKER": "vite-value"');
	});
});

describe('e2e dev server', () => {
	it('rewrites Origin only for requests from the dev server itself', () => {
		expect(backendOriginForOwnDevRequest('http://127.0.0.1:5173', '127.0.0.1:5173', 'http://127.0.0.1:4310')).toBe('http://127.0.0.1:4310');
		expect(backendOriginForOwnDevRequest('https://example.test', '127.0.0.1:5173', 'http://127.0.0.1:4310')).toBeUndefined();
	});

	// A full-page reload from the dev server mid-test destroys the page the test is driving.
	const serverOf = () => viteConfig({ mode: 'development', command: 'serve' }).server;

	it('turns HMR off only when the e2e harness asks', () => {
		vi.stubEnv('CORSIA_E2E_NO_HMR', '1');
		expect(serverOf()?.hmr).toBe(false);
		vi.stubEnv('CORSIA_E2E_NO_HMR', '');
		expect(serverOf()?.hmr).toBeUndefined();
		vi.unstubAllEnvs();
	});
});
