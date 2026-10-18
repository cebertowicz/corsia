// Checks that the startup shell never spins forever. The app's launch-time start can end before the page has
// loaded (a refusal is decided in microseconds), when the script it sends the page is lost; the page must still
// show that outcome. And a start that neither answers nor reports progress lands on the failure screen and its
// actions within the wait limit.
//
// usage: node desktop/tests/launch-outcome-shown.mjs [--shell <dist-shell dir>]
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const { chromium } = createRequire(path.join(process.env.PLAYWRIGHT_FROM ?? repo, 'frontend/package.json'))('playwright');
const flag = process.argv.indexOf('--shell');
const shell = flag > -1 ? path.resolve(process.argv[flag + 1]) : path.join(repo, 'desktop/dist-shell');
const shellUrl = pathToFileURL(path.join(shell, 'index.html')).href;

const refusal = 'A file named ~/Corsia is in the way of CORSIA’s default workspace folder. Nothing was changed.';
let failed = 0;
const fail = (message) => {
	failed += 1;
	console.error(`FAIL ${message}`);
};

const browser = await chromium.launch();

// `answers` maps a command to its result: { reject } throws it, 'pending' never answers, anything else is returned.
async function open(answers) {
	const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
	await page.clock.install();
	await page.addInitScript((answers) => {
		window.__answers = answers;
		window.__calls = [];
		window.__TAURI__ = { core: { invoke: async (command) => {
			window.__calls.push(command);
			if (command === 'get_service_log_path') return '/Users/you/Library/Application Support/CORSIA/state/logs/service.log';
			const answer = window.__answers[command];
			if (answer === 'pending') return new Promise(() => {});
			if (answer?.reject) throw answer.reject;
			return answer ?? null;
		} } };
	}, answers);
	await page.goto(shellUrl, { waitUntil: 'load' });
	await page.clock.runFor(50);
	return page;
}

const read = (page) => page.evaluate(() => ({
	status: document.getElementById('status').textContent,
	line: document.getElementById('line').hidden ? null : document.getElementById('line').textContent,
	spinner: !document.getElementById('spinner').hidden,
	retry: document.getElementById('actions').hidden || document.getElementById('retry').hidden ? null : document.getElementById('retry').textContent,
	quit: !document.getElementById('actions').hidden && !(document.getElementById('quit')?.hidden ?? true),
	log: !document.getElementById('log').hidden,
}));

function expectFailure(label, seen, line) {
	if (seen.status !== 'CORSIA couldn’t start') return fail(`${label}: status ${JSON.stringify(seen.status)} (spinner ${seen.spinner})`);
	if (seen.spinner) return fail(`${label}: spinner still showing`);
	if (seen.line !== line) return fail(`${label}: line ${JSON.stringify(seen.line)}, expected ${JSON.stringify(line)}`);
	if (seen.retry !== 'Try again' || !seen.log) return fail(`${label}: actions ${JSON.stringify(seen)}`);
	console.log(`ok   ${label}`);
}

try {
	// The launch was refused before the page loaded, so no script reached it: the page reads the outcome on load.
	{
		const page = await open({ launch_outcome: { reject: refusal } });
		expectFailure('refused before the page loaded', await read(page), refusal);
	}
	// An existing install found before the page loaded is offered the same way.
	{
		const page = await open({ launch_outcome: { state: 'adoption_offered', workspace: '~/corsia' } });
		const seen = await read(page);
		if (seen.status !== 'Use the CORSIA already on this computer?' || seen.retry !== 'Use it') fail(`offered before the page loaded: ${JSON.stringify(seen)}`);
		else console.log('ok   offered before the page loaded');
	}
	// Still running when the page loaded: it shows the outcome once the app says it is in.
	{
		const page = await open({});
		const waiting = await read(page);
		if (waiting.status !== 'Starting CORSIA…' || !waiting.spinner) fail(`pending launch: ${JSON.stringify(waiting)}`);
		await page.evaluate((reason) => {
			window.__answers.launch_outcome = { reject: reason };
			window.__corsiaShowLaunchOutcome?.();
		}, refusal);
		await page.clock.runFor(50);
		expectFailure('refused after the page loaded', await read(page), refusal);
	}
	// A launch that never answers: the spinner holds until the limit, then the failure screen takes over.
	{
		const page = await open({});
		await page.clock.runFor(59_000);
		const before = await read(page);
		if (before.status !== 'Starting CORSIA…' || !before.spinner) fail(`silent launch at 59 s: ${JSON.stringify(before)}`);
		await page.clock.runFor(1_500);
		expectFailure('silent launch at the limit', await read(page), null);
		// Its Try again starts the service; a start that never answers ends the same way.
		await page.evaluate(() => { window.__answers.start_local_service = 'pending'; });
		await page.click('#retry');
		await page.clock.runFor(50);
		const retrying = await read(page);
		if (retrying.status !== 'Starting CORSIA…' || !retrying.spinner) fail(`retry: ${JSON.stringify(retrying)}`);
		await page.clock.runFor(61_000);
		expectFailure('silent retry at the limit', await read(page), null);
		const calls = (await page.evaluate(() => window.__calls)).filter((command) => !['get_service_log_path', 'adoption_progress'].includes(command));
		if (JSON.stringify(calls) !== JSON.stringify(['launch_outcome', 'start_local_service'])) fail(`silent launch called ${JSON.stringify(calls)}`);
	}
	// A start that keeps reporting progress is working, not stuck: the limit counts from the last report.
	{
		const page = await open({ launch_outcome: { reject: refusal } });
		await page.evaluate(() => {
			window.__answers.start_local_service = 'pending';
			window.__answers.adoption_progress = { phase: 'waiting', running: 2, quit_pending: false };
		});
		await page.click('#retry');
		await page.clock.runFor(120_000);
		const working = await read(page);
		if (working.status !== 'Waiting for running work to finish…' || !working.spinner) fail(`progressing start: ${JSON.stringify(working)}`);
		else console.log('ok   progressing start keeps its spinner');
	}
	// The shell opened for a quit failure or background approval reads no launch outcome.
	for (const hash of ['#quit-failure', '#background-approval']) {
		const page = await (await browser.newContext()).newPage();
		await page.addInitScript(() => {
			window.__calls = [];
			window.__TAURI__ = { core: { invoke: async (command) => { window.__calls.push(command); return null; } } };
		});
		await page.goto(`${shellUrl}${hash}`, { waitUntil: 'load' });
		if ((await page.evaluate(() => window.__calls)).includes('launch_outcome')) fail(`${hash}: read the launch outcome`);
		else console.log(`ok   ${hash} reads no launch outcome`);
	}
} finally {
	await browser.close();
}
if (failed) {
	console.error(`${failed} check(s) failed`);
	process.exit(1);
}
