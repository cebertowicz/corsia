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
	[{ kind: 'unreachable', name: 'Studio', cause: 'local' }, { status: 'Can’t open Studio', line: 'couldn’t finish opening Studio. Try again. If it keeps failing, quit and reopen CORSIA', retry: 'Try again', setUpLocal: true }],
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

	// Each failure's buttons reach their action. Every command answers with a recorded call; disconnect_remote
	// answers "needs approval" so the page stays put and the shell's own handling of the startup is visible too.
	const shellUrl = pathToFileURL(path.join(repo, 'desktop/dist-shell/index.html')).href;
	async function press(state, button, answers = {}) {
		const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
		await page.addInitScript((answers) => {
			window.__calls = [];
			window.__TAURI__ = { core: { invoke: async (command) => {
				window.__calls.push(command);
				const answer = answers[command];
				if (answer?.reject) throw answer.reject;
				return answer ?? (command === 'disconnect_remote' ? { state: 'requires_approval' } : undefined);
			} } };
			window.setTimeout = () => 0;
		}, answers);
		await page.goto(shellUrl, { waitUntil: 'load' });
		await page.evaluate((value) => window.__corsiaShowRemoteState(value), state);
		await page.click(button);
		await page.waitForTimeout(50);
		const seen = await page.evaluate(() => ({
			calls: window.__calls,
			status: document.getElementById('status').textContent,
			retry: document.getElementById('actions').hidden ? null : document.getElementById('retry').textContent,
		}));
		return { page, ...seen };
	}
	const expectCalls = (label, seen, calls) => {
		if (JSON.stringify(seen.calls) !== JSON.stringify(calls)) throw new Error(`${label}: invoked ${JSON.stringify(seen.calls)}, expected ${JSON.stringify(calls)}`);
	};
	// The failure card looks up the service log's location on its own; it is not one of the actions under test.
	const notLogLookup = (command) => command !== 'get_service_log_path';
	let clicks = 0;
	for (const kind of ['unreachable', 'incompatible', 'too_old', 'key_changed']) {
		const state = { kind, name: 'Studio', cause: 'network' };
		const primary = await press(state, '#retry');
		// Try again retries the saved remote; Pair again forgets it and starts this computer's own pairing flow.
		expectCalls(`${kind}: primary button`, primary, [kind === 'key_changed' ? 'disconnect_remote' : 'retry_saved_remote']);
		if (kind === 'key_changed' && primary.status !== 'Keep CORSIA running') throw new Error(`${kind}: Pair again did not continue to local setup (${primary.status})`);
		const setUp = await press(state, '#set-up-local');
		expectCalls(`${kind}: Set up this computer instead`, setUp, ['disconnect_remote']);
		if (setUp.status !== 'Keep CORSIA running') throw new Error(`${kind}: Set up this computer instead did not continue to local setup (${setUp.status})`);
		clicks += 2;
	}
	// The local card's Try again retries this computer's own connect like the other cards, and never says the other computer is not listening.
	const localCard = await press({ kind: 'unreachable', name: 'Studio', cause: 'local' }, '#retry');
	expectCalls('local: primary button', localCard, ['retry_saved_remote']);
	clicks += 1;
	// A failed set-up stays on a card with an action that tries it again, never a dead end.
	const failedSetUp = await press({ kind: 'unreachable', name: 'Studio', cause: 'network' }, '#set-up-local', { disconnect_remote: { reject: 'The service could not start.' } });
	if (failedSetUp.status !== 'CORSIA couldn’t start' || failedSetUp.retry !== 'Try again') throw new Error(`failed set-up: ${failedSetUp.status} / ${failedSetUp.retry}`);
	await failedSetUp.page.click('#retry');
	await failedSetUp.page.waitForTimeout(50);
	expectCalls('failed set-up, Try again', { calls: (await failedSetUp.page.evaluate(() => window.__calls)).filter(notLogLookup) }, ['disconnect_remote', 'disconnect_remote']);
	// A retry that cannot find the saved remote falls back to the plain local start instead of sticking.
	const lostRemote = await press({ kind: 'unreachable', name: 'Studio', cause: 'network' }, '#retry', { retry_saved_remote: { reject: 'no remote is saved on this computer' } });
	if (lostRemote.status !== 'CORSIA couldn’t start') throw new Error(`lost remote: ${lostRemote.status}`);
	await lostRemote.page.click('#retry');
	await lostRemote.page.waitForTimeout(50);
	expectCalls('lost remote, Try again', { calls: (await lostRemote.page.evaluate(() => window.__calls)).filter(notLogLookup) }, ['retry_saved_remote', 'start_local_service']);
	// A disconnect that forgot the remote but could not bring this computer's CORSIA back returns to the shell with
	// this card; its action starts this computer's own service.
	const returned = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
	await returned.addInitScript(() => {
		window.__calls = [];
		window.__TAURI__ = { core: { invoke: async (command) => { window.__calls.push(command); return undefined; } } };
	});
	await returned.goto(shellUrl, { waitUntil: 'load' });
	await returned.evaluate(() => window.__corsiaShowServiceFailure('CORSIA service registration did not persist'));
	const card = await returned.evaluate(() => ({
		status: document.getElementById('status').textContent,
		line: document.getElementById('line').textContent,
		retry: document.getElementById('actions').hidden ? null : document.getElementById('retry').textContent,
	}));
	if (card.status !== 'CORSIA couldn’t start' || !card.line.includes('did not persist') || card.retry !== 'Try again') throw new Error(`returned failure card: ${JSON.stringify(card)}`);
	await returned.click('#retry');
	await returned.waitForTimeout(50);
	expectCalls('returned failure card, Try again', { calls: (await returned.evaluate(() => window.__calls)).filter(notLogLookup) }, ['start_local_service']);
	console.log(`remote launch actions: ${clicks + 5} clicks reached their action`);
} finally {
	await browser.close();
}
