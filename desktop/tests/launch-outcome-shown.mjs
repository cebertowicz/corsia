// Checks how the startup shell shows the app's own start at launch. The app keeps that start's outcome as shell
// state and draws it on every shell load (the Rust tests check that the state stays and which script draws it); this
// checks that the page draws each outcome it is handed, and that a start still working keeps its spinner rather
// than being called a failure by the page.
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

const refusal = 'A file named ~/Corsia is in the way of CORSIA’s default workspace folder.';
let failed = 0;
const fail = (message) => {
	failed += 1;
	console.error(`FAIL ${message}`);
};

const browser = await chromium.launch();

// `progress` is what the app's start at launch reports to `adoption_progress` (null: nothing to show).
async function open(progress = null) {
	const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
	await page.clock.install();
	await page.addInitScript((progress) => {
		window.__progress = progress;
		window.__calls = [];
		window.__TAURI__ = { core: { invoke: async (command) => {
			window.__calls.push(command);
			if (command === 'get_service_log_path') return '/Users/you/Library/Application Support/CORSIA/state/logs/service.log';
			if (command === 'start_fresh_preview') return { moves: ['~/Corsia'], code: ['~/.corsia'], work: [], backups: '~/CORSIA backups' };
			if (command === 'adoption_progress') return window.__progress;
			return null;
		} } };
	}, progress);
	await page.goto(shellUrl, { waitUntil: 'load' });
	await page.clock.runFor(50);
	return page;
}

const read = (page) => page.evaluate(() => ({
	status: document.getElementById('status').textContent,
	line: document.getElementById('line').hidden ? null : document.getElementById('line').textContent,
	spinner: !document.getElementById('spinner').hidden,
	retry: document.getElementById('actions').hidden || document.getElementById('retry').hidden ? null : document.getElementById('retry').textContent,
}));

// The scripts the app's page-load hook runs for each launch outcome (shell_state_script in lib.rs).
const drawn = {
	// An earlier install this app can't open is offered Start fresh.
	refused: `window.__corsiaShowStartup?.(${JSON.stringify({ state: 'existing_install', reason: refusal })});`,
	offered: 'window.__corsiaShowStartup?.({"state":"adoption_offered","workspace":"~/corsia"});',
	approval: 'window.__corsiaShowStartup?.({"state":"requires_approval"});',
	ready: 'window.__corsiaShowStartup?.({"state":"ready","url":"http://127.0.0.1:47895/"});',
	// An earlier adoption's undo that did not finish, found by the launch start.
	undoUnfinished: 'window.__corsiaShowStartup?.({"state":"adoption_failed","failure":{"message":"the old install did not answer","outcome":"unconfirmed","recovery":"start_again","advice":false}});',
};

try {
	// A start still working well past a minute is not the page's to call failed: the page draws only what the app answers.
	{
		const page = await open();
		await page.clock.runFor(120_000);
		const seen = await read(page);
		if (seen.status !== 'Starting CORSIA…' || !seen.spinner) fail(`working launch at 120 s: ${JSON.stringify(seen)}`);
		else console.log('ok   a launch still working keeps its spinner');
		const started = (await page.evaluate(() => window.__calls)).filter((command) => command === 'start_local_service');
		if (started.length) fail(`working launch: the page started the service itself ${started.length} time(s)`);
	}
	// Each launch outcome the app draws lands on its screen.
	{
		const page = await open();
		await page.evaluate(drawn.refused);
		await page.clock.runFor(50);
		const seen = await read(page);
		const offer = 'A file named ~/Corsia is in the way of CORSIA’s default workspace folder. Start fresh stops your earlier install, then moves this into a new folder in ~/CORSIA backups. Nothing is deleted.';
		if (seen.status !== 'Start CORSIA fresh?' || seen.line !== offer || seen.retry !== 'Start fresh') fail(`refused: ${JSON.stringify(seen)}`);
		else console.log('ok   refused launch shows its reason and Start fresh');
	}
	{
		const page = await open();
		await page.evaluate(drawn.offered);
		await page.clock.runFor(50);
		const seen = await read(page);
		if (seen.status !== 'Use the CORSIA already on this computer?' || seen.retry !== 'Take over') fail(`offered: ${JSON.stringify(seen)}`);
		else console.log('ok   offered install is asked about');
	}
	{
		const page = await open();
		await page.evaluate(drawn.approval);
		await page.clock.runFor(50);
		const seen = await read(page);
		if (seen.status !== 'Keep CORSIA running' || seen.spinner) fail(`approval: ${JSON.stringify(seen)}`);
		else console.log('ok   approval needed is asked for');
	}
	{
		const page = await open();
		await page.evaluate(drawn.undoUnfinished);
		await page.clock.runFor(50);
		const seen = await read(page);
		if (seen.spinner || !seen.retry || seen.status === 'Starting CORSIA…') fail(`undo unfinished: ${JSON.stringify(seen)}`);
		else console.log(`ok   an unfinished undo is drawn with its recovery (${seen.retry})`);
	}
	// The one-time data folder move runs inside the launch start: the page shows its one percentage and bar,
	// then the drawn outcome replaces it.
	{
		const page = await open({ phase: 'moving_data', percent: 42, quit_pending: false });
		await page.clock.runFor(1_100);
		const bar = () => page.evaluate(() => ({
			percent: document.getElementById('move-percent').hidden ? null : document.getElementById('move-percent').textContent,
			bar: document.getElementById('move-bar').hidden ? null : document.getElementById('move-bar').getAttribute('aria-valuenow'),
			width: document.querySelector('#move-bar i').style.width,
		}));
		const seen = { ...(await read(page)), ...(await bar()) };
		const expected = {
			status: 'Moving CORSIA’s data to its new folder',
			line: 'This happens once. CORSIA stops while it moves, and any job an agent left running is stopped.',
			spinner: false,
			retry: null,
			percent: '42%',
			bar: '42',
			width: '42%',
		};
		if (JSON.stringify(seen) !== JSON.stringify(expected)) fail(`moving data: ${JSON.stringify(seen)}`);
		else console.log('ok   the data folder move shows its percentage during the launch start');
		if (process.argv.includes('--screenshots')) await page.screenshot({ path: process.argv[process.argv.indexOf('--screenshots') + 1] });
		await page.evaluate(drawn.refused);
		await page.clock.runFor(2_100);
		const after = { ...(await read(page)), ...(await bar()) };
		if (after.percent !== null || after.bar !== null || after.status !== 'Start CORSIA fresh?') fail(`moving data then outcome: ${JSON.stringify(after)}`);
		else console.log('ok   a drawn launch outcome replaces the move and stops following it');
	}
	// A service already running answers Ready before the page is up; the load hook's script must still open it.
	{
		const page = await open();
		await page.route('http://127.0.0.1:47895/**', (route) => route.fulfill({ contentType: 'text/html', body: '<title>service</title>' }));
		const opened = page.waitForURL('http://127.0.0.1:47895/', { timeout: 5_000 }).then(() => true, () => false);
		await page.evaluate(drawn.ready);
		await page.clock.runFor(1_000);
		if (!(await opened)) fail(`ready: the page stayed on ${page.url()}`);
		else console.log('ok   ready launch opens the service');
	}
} finally {
	await browser.close();
}
if (failed) {
	console.error(`${failed} check(s) failed`);
	process.exit(1);
}
