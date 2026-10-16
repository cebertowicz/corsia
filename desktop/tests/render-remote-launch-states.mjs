import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const require = createRequire(path.join(repo, 'frontend/package.json'));
const { chromium } = require('playwright');

// Each remote launch state, drawn by the shell page's own hook, with the text and actions it must show.
const states = [
	[{ kind: 'connecting', name: 'Studio' }, { status: 'Connecting to Studio…', spinner: true, retry: null, setUpLocal: false }],
	[{ kind: 'unreachable', name: 'Studio', cause: 'network' }, { status: 'Can’t open Studio', line: 'Check your network', retry: 'Try again', setUpLocal: true }],
	[{ kind: 'unreachable', name: 'Studio', cause: 'server' }, { status: 'Can’t open Studio', line: 'Open CORSIA › Settings › System', retry: 'Try again', setUpLocal: true }],
	[{ kind: 'incompatible', name: 'Studio', app: '0.4.1', host: '0.3.0' }, { status: 'Can’t open Studio', line: 'CORSIA 0.4.1 and Studio has 0.3.0', retry: 'Try again', setUpLocal: true }],
	[{ kind: 'too_old', name: 'Studio' }, { status: 'Can’t open Studio', line: 'Studio has an older CORSIA', retry: 'Try again', setUpLocal: true }],
	[{ kind: 'key_changed', name: 'Studio' }, { status: 'Studio has changed', line: 'isn’t the computer this app paired with', retry: 'Pair again', setUpLocal: true }],
];

const browser = await chromium.launch();
try {
	for (const colorScheme of ['light', 'dark']) {
		const page = await (await browser.newContext({ colorScheme, viewport: { width: 1280, height: 900 } })).newPage();
		await page.addInitScript(() => {
			window.__TAURI__ = { core: { invoke: async () => undefined } };
			window.setTimeout = () => 0;
		});
		await page.goto(pathToFileURL(path.join(repo, 'desktop/dist-shell/index.html')).href, { waitUntil: 'load' });
		for (const [state, expected] of states) {
			await page.evaluate((value) => window.__corsiaShowRemoteState(value), state);
			const seen = await page.evaluate(() => ({
				status: document.getElementById('status').textContent,
				line: document.getElementById('line').hidden ? '' : document.getElementById('line').textContent,
				spinner: !document.getElementById('spinner').hidden,
				retry: document.getElementById('actions').hidden ? null : document.getElementById('retry').textContent,
				setUpLocal: !document.getElementById('set-up-local').hidden,
				noScroll: document.documentElement.scrollHeight <= window.innerHeight,
			}));
			const label = `${state.kind}/${state.cause ?? ''} (${colorScheme})`;
			if (seen.status !== expected.status) throw new Error(`${label}: status ${JSON.stringify(seen.status)}`);
			if (expected.line && !seen.line.includes(expected.line)) throw new Error(`${label}: line ${JSON.stringify(seen.line)}`);
			if ('spinner' in expected && seen.spinner !== expected.spinner) throw new Error(`${label}: spinner ${seen.spinner}`);
			if (seen.retry !== expected.retry) throw new Error(`${label}: primary action ${JSON.stringify(seen.retry)}`);
			if (seen.setUpLocal !== expected.setUpLocal) throw new Error(`${label}: Set up this computer instead shown=${seen.setUpLocal}`);
			if (!seen.noScroll) throw new Error(`${label}: page scrolls`);
		}
	}
	console.log(`remote launch states: ${states.length * 2} renders passed`);
} finally {
	await browser.close();
}
