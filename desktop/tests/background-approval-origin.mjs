// Checks that only the app starts the startup shell's background-approval wait. The wait polls start_local_service
// until the person approves, so it may begin only from the app's own startup answer (requires_approval, drawn by the
// page-load hook), never from a URL fragment anything able to navigate the window could add.
//
// usage: node desktop/tests/background-approval-origin.mjs [--shell <dist-shell dir>]
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const { chromium } = createRequire(path.join(process.env.PLAYWRIGHT_FROM ?? repo, 'frontend/package.json'))('playwright');
const flag = process.argv.indexOf('--shell');
const shell = flag > -1 ? path.resolve(process.argv[flag + 1]) : path.join(repo, 'desktop/dist-shell');
const shellUrl = pathToFileURL(path.join(shell, 'index.html')).href;

let failed = 0;
const fail = (message) => {
	failed += 1;
	console.error(`FAIL ${message}`);
};

const browser = await chromium.launch();

async function open(url) {
	const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
	await page.clock.install();
	await page.addInitScript(() => {
		window.__calls = [];
		window.__TAURI__ = { core: { invoke: async (command) => {
			window.__calls.push(command);
			if (command === 'start_local_service') return { state: 'requires_approval' };
			return null;
		} } };
	});
	await page.goto(url, { waitUntil: 'load' });
	await page.clock.runFor(50);
	return page;
}

const starts = (page) => page.evaluate(() => window.__calls.filter((command) => command === 'start_local_service').length);
const approvalShown = (page) => page.evaluate(() => !document.getElementById('background-approval').hidden);

try {
	// A forged fragment draws nothing and starts nothing.
	const forged = await open(`${shellUrl}#background-approval`);
	await forged.clock.runFor(5000);
	if (await starts(forged)) fail(`a #background-approval fragment started the service ${await starts(forged)} time(s)`);
	if (await approvalShown(forged)) fail('a #background-approval fragment drew the approval wait');

	// The app's own answer still starts the wait, which asks again each second until approved.
	await forged.evaluate(() => window.__corsiaShowStartup({ state: 'requires_approval' }));
	await forged.clock.runFor(3100);
	if (!(await approvalShown(forged))) fail('the app\'s requires_approval answer did not draw the approval wait');
	if ((await starts(forged)) < 2) fail(`the approval wait polled start_local_service ${await starts(forged)} time(s), not each second`);
} finally {
	await browser.close();
}

if (failed) process.exit(1);
console.log('ok: only the app\'s startup answer starts the background-approval wait');
