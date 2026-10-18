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

const refusal = 'A file named ~/Corsia is in the way of CORSIA’s default workspace folder. Nothing was changed.';
let failed = 0;
const fail = (message) => {
	failed += 1;
	console.error(`FAIL ${message}`);
};

const browser = await chromium.launch();

async function open() {
	const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
	await page.clock.install();
	await page.addInitScript(() => {
		window.__calls = [];
		window.__TAURI__ = { core: { invoke: async (command) => {
			window.__calls.push(command);
			if (command === 'get_service_log_path') return '/Users/you/Library/Application Support/CORSIA/state/logs/service.log';
			return null;
		} } };
	});
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
	refused: `window.__corsiaShowServiceFailure?.(${JSON.stringify(refusal)});`,
	offered: 'window.__corsiaShowStartup?.({"state":"adoption_offered","workspace":"~/corsia"});',
	approval: 'window.__corsiaShowStartup?.({"state":"requires_approval"});',
	ready: 'window.__corsiaShowStartup?.({"state":"ready","url":"http://127.0.0.1:47895/"});',
	// A Check again that failed, kept so a reload draws it again.
	switched: 'window.__corsiaShowStartup?.({"state":"adoption_failed","failure":{"message":"no answer on 47895","outcome":"switched","recovery":null,"advice":false}});',
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
		if (seen.status !== 'CORSIA couldn’t start' || seen.line !== refusal || seen.retry !== 'Try again') fail(`refused: ${JSON.stringify(seen)}`);
		else console.log('ok   refused launch shows its reason and Try again');
	}
	{
		const page = await open();
		await page.evaluate(drawn.offered);
		await page.clock.runFor(50);
		const seen = await read(page);
		if (seen.status !== 'Use the CORSIA already on this computer?' || seen.retry !== 'Use it') fail(`offered: ${JSON.stringify(seen)}`);
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
		await page.evaluate(drawn.switched);
		await page.clock.runFor(50);
		const seen = await read(page);
		if (seen.spinner || !seen.retry || seen.status === 'Starting CORSIA…') fail(`switched: ${JSON.stringify(seen)}`);
		else console.log(`ok   a failed check again is drawn with its action (${seen.retry})`);
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
