// Walks Start fresh in the real launch shell: the initial choices fetch and show the folder preview,
// Start fresh moves it directly, and automatic setup follows a successful backup. It also covers preview refusal,
// failure recovery, the independent Take over choice, and a restored launch failure. Each action runs only its command.
//
// usage: node desktop/tests/start-fresh-journey.mjs [--screenshots <dir>]
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
const viewportName = process.env.CORSIA_SHELL_VIEWPORT === 'small' ? 'small' : 'wide';
const viewport = viewportName === 'small' ? { width: 390, height: 844 } : { width: 1280, height: 900 };
if (screenshots) await mkdir(screenshots, { recursive: true });

const types = { '.html': 'text/html', '.css': 'text/css', '.svg': 'image/svg+xml' };
const server = createServer(async (request, response) => {
	try {
		const name = new URL(request.url, 'http://127.0.0.1').pathname.replace(/^\//, '');
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

let failed = 0;
const fail = (message) => {
	failed += 1;
	console.error(`FAIL ${message}`);
};

const reason = 'CORSIA found an existing install on this computer (~/.corsia), and this app can’t open it.';
const preview = {
	moves: ['~/Corsia', '~/.corsia/config.json', '~/Library/LaunchAgents/com.corsia.prod.plist'],
	code: ['~/.corsia'],
	work: [],
	backups: '~/CORSIA backups',
};
const backup = '~/CORSIA backups/2026-10-22 13-05';

// `answers` maps a command to what it returns; a function answer receives the call count and may throw.
async function open(theme, answers) {
	const page = await browser.newPage({ viewport, colorScheme: theme });
	await page.exposeFunction('__answer', async (command, count) => {
		const answer = typeof answers[command] === 'function' ? await answers[command](count) : answers[command];
		if (answer && answer.throws) return { throws: answer.throws };
		return { value: answer ?? null };
	});
	await page.addInitScript(() => {
		window.__calls = [];
		window.__TAURI__ = { core: { invoke: async (command) => {
			window.__calls.push(command);
			const count = window.__calls.filter((made) => made === command).length;
			const { value, throws } = await window.__answer(command, count);
			if (throws) throw throws;
			if (value && value.pending) return new Promise(() => {});
			return value;
		} } };
	});
	await page.goto(`http://127.0.0.1:${server.address().port}/index.html`, { waitUntil: 'networkidle' });
	return page;
}

const read = (page) => page.evaluate(() => {
	const visible = (id) => {
		const element = document.getElementById(id);
		return element && !element.hidden ? element.textContent : null;
	};
	return {
		title: document.getElementById('status').textContent,
		line: visible('line'),
		note: visible('note'),
		list: document.getElementById('fresh-list').hidden ? null : [...document.querySelectorAll('#fresh-list li')].map((item) => item.textContent),
		details: document.getElementById('details').hidden ? null : document.getElementById('details-text').textContent,
		button: visible('retry'),
		instead: visible('start-fresh'),
		quit: !document.getElementById('quit').hidden,
		actions: !document.getElementById('actions').hidden,
	};
});

function compare(label, shown, expected) {
	for (const [key, value] of Object.entries(expected)) {
		if (JSON.stringify(shown[key]) !== JSON.stringify(value)) {
			fail(`${label}: ${key} is ${JSON.stringify(shown[key])}, expected ${JSON.stringify(value)}`);
		}
	}
}

const made = async (page) => (await page.evaluate(() => window.__calls)).filter((command) => !['get_service_log_path', 'adoption_progress'].includes(command));

async function shot(page, theme, name) {
	if (screenshots) await page.screenshot({ path: path.join(screenshots, `${theme}-${viewportName}-${name}.png`) });
}

for (const theme of ['dark', 'light']) {
	// The offer, confirmation, move and automatic setup, end to end.
	{
		const page = await open(theme, { start_fresh_preview: preview, start_fresh: backup, start_local_service: { pending: true } });
		const before = failed;
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		compare(`${theme} offer`, await read(page), {
			title: 'Start CORSIA fresh?',
			line: 'CORSIA found an existing install on this computer (~/.corsia), and this app can’t open it. Start fresh stops your earlier install, then moves these into a new folder in ~/CORSIA backups. Nothing is deleted.',
			note: 'Its code folder stays where it is: ~/.corsia.',
			list: preview.moves,
			details: null,
			button: 'Start fresh',
			instead: null,
			quit: true,
		});
		await shot(page, theme, '1-offer');
		// A cause in the earlier install's own terms stays behind Details, never in the line.
		const cause = 'source service plist has no valid port';
		const plistReason = 'CORSIA found an existing install on this computer, and this app can’t take it over.';
		await page.evaluate(([said, why]) => window.__corsiaShowStartup({ state: 'existing_install', reason: said, details: why }), [plistReason, cause]);
		compare(`${theme} offer with a cause`, await read(page), {
			line: `${plistReason} Start fresh stops your earlier install, then moves these into a new folder in ~/CORSIA backups. Nothing is deleted.`,
			list: preview.moves,
			details: cause,
			button: 'Start fresh',
		});
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		if (await page.getByText('Move and start fresh', { exact: true }).count()) fail(`${theme} choice: showed a second confirmation action`);
		await page.evaluate(() => { document.getElementById('retry').click(); document.getElementById('retry').click(); });
		await page.waitForFunction(() => window.__calls.filter((command) => command === 'start_local_service').length === 1);
		await shot(page, theme, '2-direct-start');
		compare(`${theme} starting`, await read(page), {
			title: 'Starting CORSIA…',
			line: null,
			note: null,
			list: null,
			details: null,
			actions: false,
		});
		await shot(page, theme, '4-starting');
		const calls = await made(page);
		const expected = ['start_fresh_preview', 'start_fresh_preview', 'start_fresh_preview', 'start_fresh', 'start_local_service'];
		if (JSON.stringify(calls) !== JSON.stringify(expected)) fail(`${theme} journey: ran ${JSON.stringify(calls)}, expected ${JSON.stringify(expected)}`);
		if (failed === before) console.log(`ok   ${theme} previewed choice, move, setup starts`);
		await page.close();
	}

	// Beside a takeover, Start fresh has a valid preview but Take over remains an independent main action.
	{
		const page = await open(theme, { start_fresh_preview: preview, adopt_existing_install: { pending: true } });
		const before = failed;
		await page.evaluate(() => window.__corsiaShowStartup({ state: 'adoption_offered', workspace: '~/Corsia' }));
		compare(`${theme} takeover`, await read(page), { button: 'Take over', instead: 'Start fresh instead', list: preview.moves, quit: true });
		await shot(page, theme, '0-takeover');
		await page.click('#retry');
		await page.waitForFunction(() => window.__calls.includes('adopt_existing_install'));
		if (JSON.stringify(await made(page)) !== JSON.stringify(['start_fresh_preview', 'adopt_existing_install'])) fail(`${theme} takeover: Take over ran ${JSON.stringify(await made(page))}`);
		if (failed === before) console.log(`ok   ${theme} takeover remains independent of preview`);
		await page.close();
	}

	// Start fresh from the takeover choice runs the backup and automatic setup directly, without a confirmation card.
	{
		const page = await open(theme, { start_fresh_preview: preview, start_fresh: backup, start_local_service: { pending: true } });
		const before = failed;
		await page.evaluate(() => window.__corsiaShowStartup({ state: 'adoption_offered', workspace: '~/Corsia' }));
		await page.click('#start-fresh');
		await page.waitForFunction(() => window.__calls.filter((command) => command === 'start_local_service').length === 1);
		if (await page.getByText('Move and start fresh', { exact: true }).count()) fail(`${theme} takeover fresh: showed a second confirmation action`);
		const calls = await made(page);
		if (JSON.stringify(calls) !== JSON.stringify(['start_fresh_preview', 'start_fresh', 'start_local_service'])) fail(`${theme} takeover fresh: ran ${JSON.stringify(calls)}`);
		if (failed === before) console.log(`ok   ${theme} takeover Start fresh moves once and starts setup`);
		await page.close();
	}

	// After Start fresh moves, a new takeover choice gets its own valid or refused preview rather than staying pending.
	for (const { name, nextPreview, instead } of [
		{ name: 'valid preview', nextPreview: preview, instead: 'Start fresh instead' },
		{ name: 'refused preview', nextPreview: { throws: { message: 'CORSIA can’t inspect this earlier install.', details: 'permission denied' } }, instead: 'Check folders again' },
	]) {
		const page = await open(theme, {
			start_fresh_preview: (count) => count === 1 ? preview : nextPreview,
			start_fresh: backup,
			start_local_service: { state: 'adoption_offered', workspace: '~/Corsia' },
		});
		const before = failed;
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		await page.click('#retry');
		await page.waitForFunction((expected) => document.getElementById('start-fresh').textContent === expected, instead);
		compare(`${theme} post-fresh ${name}`, await read(page), { button: 'Take over', instead, list: name === 'valid preview' ? preview.moves : null, quit: true });
		const calls = await made(page);
		if (JSON.stringify(calls) !== JSON.stringify(['start_fresh_preview', 'start_fresh', 'start_local_service', 'start_fresh_preview'])) fail(`${theme} post-fresh ${name}: ran ${JSON.stringify(calls)}`);
		if (failed === before) console.log(`ok   ${theme} post-fresh ${name} is not stuck checking folders`);
		await page.close();
	}

	// A rejected preview never allows movement and leaves Take over usable.
	{
		const refusal = { message: 'CORSIA can’t inspect this earlier install.', details: 'permission denied', restored: true };
		const page = await open(theme, { start_fresh_preview: (count) => count === 1 ? { throws: refusal } : preview, adopt_existing_install: { pending: true } });
		const before = failed;
		await page.evaluate(() => window.__corsiaShowStartup({ state: 'adoption_offered', workspace: '~/Corsia' }));
		compare(`${theme} takeover preview refusal`, await read(page), { button: 'Take over', instead: 'Check folders again', list: null, quit: true });
		await page.click('#start-fresh');
		await page.waitForFunction(() => document.getElementById('start-fresh').textContent === 'Start fresh instead');
		if (JSON.stringify(await made(page)) !== JSON.stringify(['start_fresh_preview', 'start_fresh_preview'])) fail(`${theme} preview retry: moved data or ran ${JSON.stringify(await made(page))}`);
		await page.click('#retry');
		await page.waitForFunction(() => window.__calls.includes('adopt_existing_install'));
		const calls = await made(page);
		if (JSON.stringify(calls) !== JSON.stringify(['start_fresh_preview', 'start_fresh_preview', 'adopt_existing_install'])) fail(`${theme} takeover preview refusal: ran ${JSON.stringify(calls)}`);
		if (failed === before) console.log(`ok   ${theme} rejected preview keeps Take over usable`);
		await page.close();
	}

	// A late preview cannot replace Take over's progress after adoption has started.
	{
		let releasePreview;
		const delayedPreview = new Promise((resolve) => { releasePreview = resolve; });
		const page = await open(theme, { start_fresh_preview: () => delayedPreview, adopt_existing_install: { pending: true } });
		const before = failed;
		await page.evaluate(() => { void window.__corsiaShowStartup({ state: 'adoption_offered', workspace: '~/Corsia' }); });
		await page.waitForFunction(() => document.getElementById('retry').textContent === 'Take over');
		compare(`${theme} checking folders`, await read(page), { button: 'Take over', instead: 'Checking folders…', list: null, quit: true });
		await page.click('#retry');
		await page.waitForFunction(() => document.getElementById('status').textContent === 'Giving running work up to a minute to finish…');
		releasePreview(preview);
		await page.waitForTimeout(50);
		compare(`${theme} held takeover preview`, await read(page), { title: 'Giving running work up to a minute to finish…', actions: false });
		const calls = await made(page);
		if (JSON.stringify(calls) !== JSON.stringify(['start_fresh_preview', 'adopt_existing_install'])) fail(`${theme} held takeover preview: ran ${JSON.stringify(calls)}`);
		if (failed === before) console.log(`ok   ${theme} late preview cannot replace takeover progress`);
		await page.close();
	}

	// While it moves, the window shows the phase; a Quit asked for says when the app will close.
	{
		const page = await open(theme, {
			start_fresh_preview: preview,
			start_fresh: { pending: true },
			adoption_progress: { phase: 'moving_to_backup', quit_pending: true },
		});
		const before = failed;
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		await page.click('#retry');
		await page.waitForFunction(() => window.__calls.includes('adoption_progress'), null, { timeout: 4000 });
		await page.waitForTimeout(100);
		compare(`${theme} moving`, await read(page), {
			title: 'Moving your earlier install into a backup…',
			note: 'CORSIA will close once everything is in one place.',
			list: null,
			actions: false,
		});
		await shot(page, theme, '3-moving');
		if (failed === before) console.log(`ok   ${theme} moving`);
		await page.close();
	}

	// A work folder named in the settings stays, and the initial choice says so after the code folder.
	{
		const page = await open(theme, { start_fresh_preview: { ...preview, work: ['~/Developer'] } });
		const before = failed;
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		compare(`${theme} work folder`, await read(page), {
			note: 'Its code folder stays where it is: ~/.corsia. Its work folder stays where it is: ~/Developer.',
		});
		if (failed === before) console.log(`ok   ${theme} work folder stays`);
		await page.close();
	}

	// A preview failure is not a move failure: its details stay visible and checking again still moves nothing.
	for (const { name, error, details } of [
		{ name: 'object', error: { message: 'CORSIA can’t inspect this earlier install.', details: 'permission denied' }, details: 'permission denied' },
		{ name: 'scalar', error: 'preview service unavailable', details: 'preview service unavailable' },
	]) {
		const page = await open(theme, { start_fresh_preview: (count) => count === 1 ? { throws: error } : preview });
		const before = failed;
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		await page.waitForFunction(() => document.getElementById('retry').textContent === 'Check folders again');
		compare(`${theme} preview failure ${name}`, await read(page), {
			title: 'Start CORSIA fresh?',
			line: reason,
			note: 'CORSIA couldn’t check what Start fresh will move. Check folders again before starting fresh.',
			details,
			button: 'Check folders again',
			list: null,
			quit: true,
		});
		await shot(page, theme, `5-preview-${name}`);
		await page.click('#retry');
		await page.waitForFunction(() => document.getElementById('retry').textContent === 'Start fresh');
		const calls = await made(page);
		if (JSON.stringify(calls) !== JSON.stringify(['start_fresh_preview', 'start_fresh_preview'])) fail(`${theme} preview failure ${name}: moved data or ran ${JSON.stringify(calls)}`);
		if (failed === before) console.log(`ok   ${theme} preview failure ${name}`);
		await page.close();
	}

	// An unfinished Start fresh put back at launch: the earlier install didn't start, so the card says it isn't running.
	{
		const failure = {
			message: 'Everything is back where it was, but CORSIA couldn’t start your earlier install again, so it isn’t running. Choose Retry to try Start fresh again.',
			details: '`corsia start` exited 1',
			restored: true,
			left_stopped: true,
		};
		const page = await open(theme, { start_local_service: { state: 'existing_install', reason }, start_fresh_preview: preview });
		const before = failed;
		await page.evaluate((given) => window.__corsiaShowStartup({ state: 'start_fresh_failed', failure: given }), failure);
		compare(`${theme} launch undo`, await read(page), { title: 'CORSIA couldn’t start fresh', line: failure.message, details: failure.details, button: 'Retry', quit: true });
		await shot(page, theme, '6-launch-undo');
		await page.click('#retry');
		await page.waitForFunction(() => document.getElementById('status').textContent === 'Start CORSIA fresh?');
		if (failed === before) console.log(`ok   ${theme} launch undo`);
		await page.close();
	}

	// A failure that put everything back offers Retry, which reads the computer again.
	const failureTitle = 'CORSIA couldn’t start fresh';
	for (const { name, error, title = failureTitle, line, details, button, next } of [
		{
			name: 'restored',
			error: { message: 'CORSIA couldn’t move everything into the backup folder, so it put it all back where it was.', details: 'could not move ~/Corsia: Permission denied', restored: true },
			line: 'CORSIA couldn’t move everything into the backup folder, so it put it all back where it was.',
			details: 'could not move ~/Corsia: Permission denied',
			button: 'Retry',
			next: 'start_fresh_preview',
		},
		{
			name: 'not-restored',
			error: { message: 'CORSIA couldn’t put everything back, but nothing was deleted: what it moved is in ~/CORSIA backups/2026-10-22 13-05. Reopen CORSIA to try again.', details: 'could not move ~/Corsia back', restored: false },
			line: 'CORSIA couldn’t put everything back, but nothing was deleted: what it moved is in ~/CORSIA backups/2026-10-22 13-05. Reopen CORSIA to try again.',
			details: 'could not move ~/Corsia back',
			button: 'Reopen CORSIA',
			next: 'reopen_app',
		},
		// The earlier install went away before the move: setup goes on instead of a Retry that fails the same way.
		{
			name: 'nothing-to-move',
			error: { message: 'Your earlier install is no longer on this computer, so there’s nothing to move. Choose Set up CORSIA to go on.', details: 'first run finds no earlier install', restored: true, nothing_to_move: true },
			title: 'CORSIA is ready to set up',
			line: 'Your earlier install is no longer on this computer, so there’s nothing to move. Choose Set up CORSIA to go on.',
			details: 'first run finds no earlier install',
			button: 'Set up CORSIA',
			next: 'start_local_service',
		},
	]) {
		const page = await open(theme, { start_fresh_preview: preview, start_fresh: { throws: error }, start_local_service: { pending: true } });
		const before = failed;
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		await page.click('#retry');
		await page.waitForFunction((expected) => document.getElementById('status').textContent === expected, title);
		compare(`${theme} failure ${name}`, await read(page), { title, line, details, button, list: null, quit: true });
		if ((await made(page)).includes('start_local_service')) fail(`${theme} failure ${name}: started setup before the person chose recovery`);
		await shot(page, theme, `5-failure-${name}`);
		await page.click('#retry');
		await page.waitForTimeout(150);
		const calls = await made(page);
		if (calls[calls.length - 1] !== next) fail(`${theme} failure ${name}: ran ${JSON.stringify(calls)}, expected it to end with ${next}`);
		if (failed === before) console.log(`ok   ${theme} failure ${name}`);
		await page.close();
	}

	// The disk-image card asks for the move only.
	{
		const page = await open(theme, {});
		await page.evaluate(() => window.__corsiaShowStartup({ state: 'disk_image' }));
		const shown = await read(page);
		const before = failed;
		compare(`${theme} disk image`, shown, { line: 'Drag CORSIA into your Applications folder, then open it from there.', instead: null });
		if (failed === before) console.log(`ok   ${theme} disk image`);
		await page.close();
	}
}
await browser.close();
server.close();
if (failed) process.exit(1);
