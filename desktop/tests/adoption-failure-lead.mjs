// Checks the adoption failure screen in the real launch shell: what each outcome says, which
// button it offers, and that the button runs the right command and nothing else. The window reads
// the backend's `outcome` and `recovery`, never the reason text, and the reason only appears under
// Details (except a refusal written for the person, which is the line).
//
// usage: node desktop/tests/adoption-failure-lead.mjs [--screenshots <dir>]
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const shell = path.join(repo, 'desktop/dist-shell');
const { chromium } = createRequire(path.join(process.env.PLAYWRIGHT_FROM ?? repo, 'frontend/package.json'))('playwright');
const flag = process.argv.indexOf('--screenshots');
const screenshots = flag > -1 ? process.argv[flag + 1] : null;
if (screenshots) await mkdir(screenshots, { recursive: true });

const types = { '.html': 'text/html', '.css': 'text/css', '.svg': 'image/svg+xml' };
const server = createServer(async (request, response) => {
	try {
		const name = new URL(request.url, 'http://127.0.0.1').pathname.replace(/^\/(desktop\/dist-shell\/)?/, '');
		const file = path.resolve(shell, name || 'index.html');
		if (!file.startsWith(`${shell}${path.sep}`)) throw new Error('outside the shell');
		response.writeHead(200, { 'content-type': types[path.extname(file)] ?? 'text/plain' });
		response.end(await readFile(file));
	} catch {
		response.writeHead(404);
		response.end();
	}
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch();

const title = 'CORSIA couldn’t switch over';
const keys = 'These provider keys aren’t stored in CORSIA yet: OPENAI_API_KEY. Add them in CORSIA › Settings › Keys, then choose Try again.';
const space = 'The backup needs 4.2 GB free and this disk has 1.1 GB. Free up space, then choose Try again.';
const untouched = 'Nothing was changed. Your existing CORSIA is still running.';
const nothingChanged = 'Nothing was changed.';
const restored = 'Your existing CORSIA is running again, as it was.';
const startAgainLine = 'Your existing CORSIA may not be running. Choose Start it again.';
const reopenLine = 'Your existing CORSIA may not be running. Choose Reopen CORSIA to put it back.';
const reboot = 'If that doesn’t work, restart this computer.';
const holdText = 'status failed; the hold on new work could not be released: cancel failed';
const stalledText = 'recovering interrupted adoption; app service could not be stopped: new stop failed';
const startingText = 'CORSIA did not answer within 30 seconds. See the service log.';
const notRunningLine = 'Your existing CORSIA isn’t running. Start it, and this app will take it over.';
const stuckLine = 'Your existing CORSIA is stuck restarting.';
const wrongFolderLine = 'Your existing CORSIA is running from ~/old/corsia, but its settings name ~/new/corsia. Nothing was changed.';
const restartingTitle = 'Your existing CORSIA is restarting. This app will continue when it’s back.';
const quitPendingNote = 'CORSIA will close once your existing CORSIA is running again as before.';
const failedStart = '`corsia start` exited 1';
const failedStartLine = 'Your existing CORSIA may not be running. Choose Try again.';

// A backend refusal comes after the install was proven loaded, so it carries `running`.
const refused = (message) => ({ message, outcome: 'untouched', recovery: null, advice: true, running: true });
const unconfirmed = (message, recovery) => ({ message, outcome: 'unconfirmed', recovery, advice: false });
const settled = (message, outcome) => ({ message, outcome, recovery: null, advice: false });
// A refusal that is the whole line, with the action that fixes its cause (or none).
const standalone = (message, recovery) => ({ message, outcome: 'untouched', recovery, advice: true, standalone: true });
const ready = { state: 'ready', url: '/index.html#started' };

// `line` null means the line is hidden; `note` null means the secondary line is hidden; `details`
// null means Details is hidden.
const cases = [
	{ name: '3b-refused-keys', failure: refused(keys), title, line: `${untouched} ${keys}`, note: null, details: null, button: 'Try again' },
	{ name: '3c-refused-space', failure: refused(space), title, line: `${untouched} ${space}`, note: null, details: null, button: 'Try again' },
	// Nothing proved the install loaded: the lead claims only that nothing was changed.
	{ name: 'untouched-technical', failure: settled('source drain API returned HTTP 500', 'untouched'), title, line: nothingChanged, note: null, details: 'source drain API returned HTTP 500', button: 'Try again' },
	{ name: 'untouched-running-technical', failure: { ...settled('could not write the journal', 'untouched'), running: true }, title, line: untouched, note: null, details: 'could not write the journal', button: 'Try again' },
	{ name: 'after-stop', failure: settled('the new service did not become ready; the old install was restored', 'restored'), title, line: restored, note: null, details: 'the new service did not become ready; the old install was restored', button: 'Try again' },
	// The same reason text with each outcome: only the outcome decides the line.
	{ name: 'same-text-restored', failure: settled(space, 'restored'), title, line: restored, note: null, details: space, button: 'Try again' },
	{ name: '3d-start-again', failure: unconfirmed(holdText, 'start_again'), title, line: startAgainLine, note: reboot, details: holdText, button: 'Start it again' },
	{ name: '3e-reopen', failure: unconfirmed(stalledText, 'reopen'), title, line: reopenLine, note: null, details: stalledText, button: 'Reopen CORSIA' },
	// A plain error claims nothing about the existing install, so it offers the relaunch that is always safe.
	{ name: 'plain-string', failure: 'could not restart', title, line: reopenLine, note: null, details: 'could not restart', button: 'Reopen CORSIA' },
	{ name: '3f-still-starting', failure: settled(startingText, 'switched'), title: 'CORSIA switched over and is still starting.', line: null, note: null, details: startingText, button: 'Check again' },
	// Whole-line refusals: no lead about the existing install, no Details, one action or none.
	{ name: '4a-not-running', failure: standalone(notRunningLine, 'start_it'), title, line: notRunningLine, note: null, details: null, button: 'Start it' },
	{ name: '4b-stuck-restarting', failure: standalone(stuckLine, 'restart_it'), title, line: stuckLine, note: null, details: null, button: 'Restart it' },
	{ name: '4c-wrong-folder', failure: standalone(wrongFolderLine, null), title, line: wrongFolderLine, note: null, details: null, button: 'Try again' },
	{ name: '4d-failed-start', failure: unconfirmed(failedStart, 'try_again'), title, line: failedStartLine, note: null, details: failedStart, button: 'Try again' },
];

// Each recovery button runs its own command; a script says what each command does.
const actions = [
	{
		name: 'start-again-succeeds',
		failure: unconfirmed(holdText, 'start_again'),
		script: { start_existing_again: 'ok' },
		calls: ['adopt_existing_install', 'start_existing_again'],
		after: { title, line: restored, note: null, details: holdText, button: 'Try again' },
	},
	{
		name: 'start-again-fails',
		failure: unconfirmed(holdText, 'start_again'),
		script: { start_existing_again: unconfirmed('the existing install could not be started: still down', 'start_again') },
		calls: ['adopt_existing_install', 'start_existing_again'],
		after: { title, line: startAgainLine, note: reboot, details: 'the existing install could not be started: still down', button: 'Start it again' },
	},
	{
		name: 'reopen-runs',
		failure: unconfirmed(stalledText, 'reopen'),
		script: { reopen_app: 'ok' },
		calls: ['adopt_existing_install', 'reopen_app'],
		after: { title: 'Reopening CORSIA…', line: null, note: null, details: null, button: null },
	},
	{
		name: 'reopen-fails',
		failure: unconfirmed(stalledText, 'reopen'),
		script: { reopen_app: 'CORSIA couldn’t reopen itself.' },
		calls: ['adopt_existing_install', 'reopen_app'],
		after: { title, line: reopenLine, note: null, details: 'CORSIA couldn’t reopen itself.', button: 'Reopen CORSIA' },
	},
	// Start it and Restart it run the existing install's own command, then ask to take it over again.
	{
		name: 'start-it-then-adopts',
		failure: standalone(notRunningLine, 'start_it'),
		script: { start_source_install: 'ok', adopt_existing_install: [standalone(notRunningLine, 'start_it'), ready] },
		calls: ['adopt_existing_install', 'start_source_install', 'adopt_existing_install'],
		hash: '#started',
	},
	{
		name: 'start-it-fails',
		failure: standalone(notRunningLine, 'start_it'),
		script: { start_source_install: unconfirmed(failedStart, 'try_again') },
		calls: ['adopt_existing_install', 'start_source_install'],
		after: { title, line: failedStartLine, note: null, details: failedStart, button: 'Try again' },
	},
	{
		name: 'try-again-after-a-failed-start-asks-to-take-it-over-again',
		failure: unconfirmed(failedStart, 'try_again'),
		script: { adopt_existing_install: [unconfirmed(failedStart, 'try_again'), ready] },
		calls: ['adopt_existing_install', 'adopt_existing_install'],
		hash: '#started',
	},
	{
		name: 'restart-it-then-adopts',
		failure: standalone(stuckLine, 'restart_it'),
		script: { restart_source_install: 'ok', adopt_existing_install: [standalone(stuckLine, 'restart_it'), ready] },
		calls: ['adopt_existing_install', 'restart_source_install', 'adopt_existing_install'],
		hash: '#started',
	},
	{
		name: 'restart-it-fails',
		failure: standalone(stuckLine, 'restart_it'),
		script: { restart_source_install: unconfirmed('`corsia restart` exited 1', 'try_again') },
		calls: ['adopt_existing_install', 'restart_source_install'],
		after: { title, line: failedStartLine, note: null, details: '`corsia restart` exited 1', button: 'Try again' },
	},
	{
		name: 'check-again-succeeds',
		failure: settled(startingText, 'switched'),
		script: { check_service_started: { state: 'ready', url: '/index.html#started' } },
		calls: ['adopt_existing_install', 'check_service_started'],
		hash: '#started',
	},
	{
		name: 'check-again-fails',
		failure: settled(startingText, 'switched'),
		script: { check_service_started: settled('CORSIA did not answer within 30 seconds. Still no answer.', 'switched') },
		calls: ['adopt_existing_install', 'check_service_started'],
		after: { title: 'CORSIA switched over and is still starting.', line: null, note: null, details: 'CORSIA did not answer within 30 seconds. Still no answer.', button: 'Check again' },
	},
];

// The page's own reads, not an action's; the takeover offer reads what Start fresh would back up.
const pageReads = ['get_service_log_path', 'adoption_progress', 'start_fresh_preview'];

let failed = 0;
const fail = (message) => {
	failed += 1;
	console.error(`FAIL ${message}`);
};

async function open(theme, failure, script = {}) {
	const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, colorScheme: theme });
	await page.addInitScript(([thrown, scripted]) => {
		window.__calls = [];
		window.__TAURI__ = { core: { invoke: async (command) => {
			window.__calls.push(command);
			if (command === 'get_service_log_path') return '/x/service.log';
			if (command === 'adoption_progress') return null;
			if (command === 'start_fresh_preview') return { moves: ['~/work'], code: [], work: [], backups: '~/CORSIA backups' };
			if (command === 'adopt_existing_install') {
				// A script may give successive results; the last one repeats.
				const next = Array.isArray(scripted.adopt_existing_install) ? (scripted.adopt_existing_install.length > 1 ? scripted.adopt_existing_install.shift() : scripted.adopt_existing_install[0]) : thrown;
				if (next && next.state) return next;
				throw next;
			}
			if (command !== 'adopt_existing_install' && command in scripted) {
				const result = scripted[command];
				if (result === 'ok') return undefined;
				if (result && result.state) return result;
				throw result;
			}
			return undefined;
		} } };
	}, [failure, script]);
	await page.goto(`http://127.0.0.1:${server.address().port}/index.html`, { waitUntil: 'networkidle' });
	await page.evaluate(() => window.__corsiaShowStartup({ state: 'adoption_offered', workspace: '~/work' }));
	await page.click('#retry');
	await page.waitForFunction(() => document.getElementById('status').textContent.includes('switch'));
	return page;
}

async function read(page) {
	return page.evaluate(() => {
		const visible = (id) => {
			const element = document.getElementById(id);
			return element.hidden ? null : element.textContent;
		};
		const details = document.getElementById('details');
		return {
			title: document.getElementById('status').textContent,
			line: visible('line'),
			note: visible('note'),
			details: details.hidden ? null : document.getElementById('details-text').textContent,
			button: document.getElementById('actions').hidden ? null : document.getElementById('retry').textContent,
			body: document.body.innerText,
		};
	});
}

function compare(label, shown, expected) {
	for (const key of ['title', 'line', 'note', 'details', 'button']) {
		if (shown[key] !== expected[key]) fail(`${label}: ${key}\n  expected: ${JSON.stringify(expected[key])}\n  shown:    ${JSON.stringify(shown[key])}`);
	}
}

for (const theme of ['light', 'dark']) {
	for (const { name, failure, ...expected } of cases) {
		const page = await open(theme, failure);
		const shown = await read(page);
		const before = failed;
		compare(`${theme} ${name}`, shown, expected);
		// A switched-over install is not an error about the old one.
		if (name === '3f-still-starting' && /existing CORSIA/.test(shown.body)) fail(`${theme} ${name}: mentions the existing CORSIA`);
		// The raw reason never leaks into the line, except a refusal written for the person.
		const reason = typeof failure === 'string' ? failure : failure.message;
		if (shown.line && !failure.advice && shown.line.includes(reason)) fail(`${theme} ${name}: the reason is in the line`);
		if (failed === before) console.log(`ok   ${theme} ${name}`);
		if (screenshots && ['3b-refused-keys', '3c-refused-space', '3d-start-again', '3e-reopen', '3f-still-starting'].includes(name)) {
			// Details open shows the reason a person can read or send on.
			await page.evaluate(() => { document.getElementById('details').open = true; });
			await page.screenshot({ path: path.join(screenshots, `${name}-${theme}.png`) });
		}
		await page.close();
	}
	// A failed undo found at launch is shown the same way, from the cause the backend chose, and
	// Try again after a settled one asks the service to start and can find it again.
	for (const { name, failure, expected } of [
		{ name: 'launch-undo-start-again', failure: unconfirmed(holdText, 'start_again'), expected: { title, line: startAgainLine, note: reboot, details: holdText, button: 'Start it again' } },
		{ name: 'launch-undo-reopen', failure: unconfirmed(stalledText, 'reopen'), expected: { title, line: reopenLine, note: null, details: stalledText, button: 'Reopen CORSIA' } },
	]) {
		const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, colorScheme: theme });
		await page.addInitScript((scripted) => {
			window.__calls = [];
			window.__TAURI__ = { core: { invoke: async (command) => {
				window.__calls.push(command);
				if (command === 'get_service_log_path') return '/x/service.log';
				if (command === 'start_local_service') return { state: 'adoption_failed', failure: scripted };
				return undefined;
			} } };
		}, failure);
		await page.goto(`http://127.0.0.1:${server.address().port}/index.html`, { waitUntil: 'networkidle' });
		const before = failed;
		await page.evaluate((shown) => window.__corsiaShowStartup({ state: 'adoption_failed', failure: shown }), failure);
		compare(`${theme} ${name} at launch`, await read(page), expected);
		if (name === 'launch-undo-reopen') {
			// Try again after a settled failure starts the service, which finds the failed undo again.
			await page.evaluate((shown) => window.__corsiaShowStartup({ state: 'adoption_failed', failure: { ...shown, outcome: 'restored', recovery: null } }), failure);
			await page.click('#retry');
			await page.waitForFunction((text) => document.getElementById('retry').textContent === text, 'Reopen CORSIA');
			compare(`${theme} ${name} after Try again`, await read(page), expected);
		}
		if (failed === before) console.log(`ok   ${theme} ${name}`);
		await page.close();
	}
	for (const { name, failure, script, calls, after, hash } of actions) {
		const page = await open(theme, failure, script);
		const before = failed;
		await page.click('#retry');
		await page.waitForFunction((last) => window.__calls.includes(last), calls[calls.length - 1], { timeout: 3000 }).catch(() => {});
		await page.waitForTimeout(150);
		const made = (await page.evaluate(() => window.__calls)).filter((command) => !pageReads.includes(command));
		if (JSON.stringify(made) !== JSON.stringify(calls)) fail(`${theme} ${name}: ran ${JSON.stringify(made)}, expected ${JSON.stringify(calls)}`);
		if (hash && (await page.evaluate(() => window.location.hash)) !== hash) fail(`${theme} ${name}: did not continue to the service`);
		if (after) compare(`${theme} ${name}`, await read(page), after);
		if (failed === before) console.log(`ok   ${theme} ${name}`);
		await page.close();
	}
	// While the source install drains, the window shows the backend's phase. The count is display
	// only: one that is missing, zero or unreadable shows the title alone, never "0 tasks".
	for (const { name, progress, title: expectedTitle, line: expectedLine, note: expectedNote = null } of [
		{ name: 'progress-waiting-count', progress: { phase: 'waiting', running: 3 }, title: 'Giving running work up to a minute to finish…', line: '3 tasks still running' },
		{ name: 'progress-waiting-one', progress: { phase: 'waiting', running: 1 }, title: 'Giving running work up to a minute to finish…', line: '1 task still running' },
		{ name: 'progress-waiting-unread', progress: { phase: 'waiting', running: null }, title: 'Giving running work up to a minute to finish…', line: null },
		{ name: 'progress-waiting-zero', progress: { phase: 'waiting', running: 0 }, title: 'Giving running work up to a minute to finish…', line: null },
		{ name: 'progress-backing-up', progress: { phase: 'backing_up', running: null }, title: 'Backing up your data…', line: null },
		{ name: 'progress-waiting-restart', progress: { phase: 'waiting_restart', quit_pending: false }, title: restartingTitle, line: null },
		// A Quit that is waiting on the switch says when the app will close.
		{ name: 'progress-quit-pending-waiting', progress: { phase: 'waiting', running: 2, quit_pending: true }, title: 'Giving running work up to a minute to finish…', line: '2 tasks still running', note: quitPendingNote },
		{ name: 'progress-quit-pending-backing-up', progress: { phase: 'backing_up', quit_pending: true }, title: 'Backing up your data…', line: null, note: quitPendingNote },
		{ name: 'progress-starting', progress: { phase: 'starting', running: null }, title: 'Starting CORSIA…', line: null },
		{ name: 'progress-starting-existing', progress: { phase: 'starting_existing', running: null }, title: 'Starting your existing CORSIA…', line: null },
		{ name: 'progress-quit-pending-starting-existing', progress: { phase: 'starting_existing', quit_pending: true }, title: 'Starting your existing CORSIA…', line: null, note: quitPendingNote },
	]) {
		const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, colorScheme: theme });
		await page.addInitScript((reading) => {
			window.__calls = [];
			window.__TAURI__ = { core: { invoke: async (command) => {
				window.__calls.push(command);
				if (command === 'get_service_log_path') return '/x/service.log';
				if (command === 'adoption_progress') return reading;
				if (command === 'adopt_existing_install') return new Promise(() => {});
				return undefined;
			} } };
		}, progress);
		await page.goto(`http://127.0.0.1:${server.address().port}/index.html`, { waitUntil: 'networkidle' });
		await page.evaluate(() => window.__corsiaShowStartup({ state: 'adoption_offered', workspace: '~/work' }));
		await page.click('#retry');
		// The window polls once a second; wait for a reading to land.
		await page.waitForFunction(() => window.__calls.includes('adoption_progress'), null, { timeout: 4000 });
		await page.waitForTimeout(100);
		const before = failed;
		const shown = await read(page);
		compare(`${theme} ${name}`, shown, { title: expectedTitle, line: expectedLine, note: expectedNote, details: null, button: null });
		if (/\b0 tasks?\b/.test(shown.body)) fail(`${theme} ${name}: shows a zero count`);
		if (failed === before) console.log(`ok   ${theme} ${name}`);
		await page.close();
	}
	// Quit asks the app to quit; closing the window would only hide it.
	{
		const page = await open(theme, refused(keys));
		const before = failed;
		await page.click('#quit');
		await page.waitForTimeout(150);
		const made = (await page.evaluate(() => window.__calls)).filter((command) => ![...pageReads, 'adopt_existing_install'].includes(command));
		if (JSON.stringify(made) !== JSON.stringify(['quit_app'])) fail(`${theme} quit: ran ${JSON.stringify(made)}, expected ["quit_app"]`);
		if (failed === before) console.log(`ok   ${theme} quit`);
		await page.close();
	}
}
await browser.close();
server.close();
if (failed) process.exit(1);
