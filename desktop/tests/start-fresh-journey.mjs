// Walks Start fresh in the real launch shell: the offer for an install this app can't open, the
// "Start fresh instead" choice beside a takeover, the confirmation listing what moves and the code
// and work folders that stay, the move itself, the done card, and every failure card: a refusal, a
// failed move, and an undo finished at launch. Each button must run the right command and nothing else.
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
	const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, colorScheme: theme });
	await page.exposeFunction('__answer', async (command, count) => {
		const answer = answers[command];
		if (answer && answer.throws) return { throws: answer.throws };
		return { value: typeof answer === 'function' ? answer(count) : answer ?? null };
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
	if (screenshots) await page.screenshot({ path: path.join(screenshots, `${theme}-${name}.png`) });
}

for (const theme of ['dark', 'light']) {
	// The offer, the confirmation, the move and the done card, end to end.
	{
		const page = await open(theme, { start_fresh_preview: preview, start_fresh: backup, start_local_service: { pending: true } });
		const before = failed;
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		compare(`${theme} offer`, await read(page), {
			title: 'Start CORSIA fresh?',
			line: 'CORSIA found an existing install on this computer (~/.corsia), and this app can’t open it. Start fresh moves it into a backup folder and sets up CORSIA as new. Nothing is deleted.',
			note: null,
			list: null,
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
			line: `${plistReason} Start fresh moves it into a backup folder and sets up CORSIA as new. Nothing is deleted.`,
			details: cause,
			button: 'Start fresh',
		});
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		await page.click('#retry');
		await page.waitForFunction(() => document.getElementById('status').textContent === 'Move your earlier CORSIA into a backup?');
		compare(`${theme} confirm`, await read(page), {
			line: 'CORSIA stops it, then moves these into a new folder in ~/CORSIA backups. Nothing is deleted.',
			note: 'Its code folder stays where it is: ~/.corsia.',
			list: preview.moves,
			button: 'Move and start fresh',
			instead: null,
			quit: true,
		});
		await shot(page, theme, '2-confirm');
		await page.click('#retry');
		await page.waitForFunction(() => document.getElementById('status').textContent === 'Your earlier CORSIA is in a backup');
		compare(`${theme} done`, await read(page), {
			line: `It’s in ${backup}. Nothing was deleted.`,
			note: null,
			list: null,
			details: null,
			button: 'Set up CORSIA',
		});
		await shot(page, theme, '4-done');
		await page.click('#retry');
		await page.waitForTimeout(150);
		const calls = await made(page);
		const expected = ['start_fresh_preview', 'start_fresh', 'start_local_service'];
		if (JSON.stringify(calls) !== JSON.stringify(expected)) fail(`${theme} journey: ran ${JSON.stringify(calls)}, expected ${JSON.stringify(expected)}`);
		if (failed === before) console.log(`ok   ${theme} offer, confirm, move, set up`);
		await page.close();
	}

	// Beside a takeover, "Start fresh instead" opens the same confirmation; "Use it" is unchanged.
	{
		const page = await open(theme, { start_fresh_preview: preview, adopt_existing_install: { pending: true } });
		const before = failed;
		await page.evaluate(() => window.__corsiaShowStartup({ state: 'adoption_offered', workspace: '~/Corsia' }));
		compare(`${theme} takeover`, await read(page), { button: 'Use it', instead: 'Start fresh instead', quit: true });
		await shot(page, theme, '0-takeover');
		await page.click('#start-fresh');
		await page.waitForFunction(() => document.getElementById('status').textContent === 'Move your earlier CORSIA into a backup?');
		compare(`${theme} takeover confirm`, await read(page), { instead: null, button: 'Move and start fresh' });
		if (JSON.stringify(await made(page)) !== JSON.stringify(['start_fresh_preview'])) fail(`${theme} takeover: ran ${JSON.stringify(await made(page))}`);
		await page.evaluate(() => window.__corsiaShowStartup({ state: 'adoption_offered', workspace: '~/Corsia' }));
		await page.click('#retry');
		await page.waitForTimeout(150);
		if (!(await made(page)).includes('adopt_existing_install')) fail(`${theme} takeover: Use it no longer takes over`);
		if (failed === before) console.log(`ok   ${theme} start fresh instead of a takeover`);
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
		await page.waitForFunction(() => document.getElementById('retry').textContent === 'Move and start fresh');
		await page.click('#retry');
		await page.waitForFunction(() => window.__calls.includes('adoption_progress'), null, { timeout: 4000 });
		await page.waitForTimeout(100);
		compare(`${theme} moving`, await read(page), {
			title: 'Moving your earlier CORSIA into a backup…',
			note: 'CORSIA will close once everything is in one place.',
			list: null,
			actions: false,
		});
		await shot(page, theme, '3-moving');
		if (failed === before) console.log(`ok   ${theme} moving`);
		await page.close();
	}

	// A work folder named in the settings stays, and the confirmation says so after the code folder.
	{
		const page = await open(theme, { start_fresh_preview: { ...preview, work: ['~/Developer'] } });
		const before = failed;
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		await page.click('#retry');
		await page.waitForFunction(() => document.getElementById('retry').textContent === 'Move and start fresh');
		compare(`${theme} work folder`, await read(page), {
			note: 'Its code folder stays where it is: ~/.corsia. Its work folder stays where it is: ~/Developer.',
		});
		if (failed === before) console.log(`ok   ${theme} work folder stays`);
		await page.close();
	}

	// A refusal says what the person can do; Retry reads the computer again.
	{
		const refusal = 'CORSIA can’t move ~/Corsia because it’s on another disk.';
		const page = await open(theme, { start_fresh_preview: { throws: { message: refusal, details: '', restored: true } } });
		const before = failed;
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		await page.click('#retry');
		await page.waitForFunction(() => document.getElementById('status').textContent === 'CORSIA couldn’t start fresh');
		compare(`${theme} refusal`, await read(page), { line: refusal, details: null, button: 'Retry', list: null, quit: true });
		await shot(page, theme, '5-refusal');
		await page.click('#retry');
		await page.waitForTimeout(150);
		const calls = await made(page);
		if (JSON.stringify(calls) !== JSON.stringify(['start_fresh_preview', 'start_fresh_preview'])) fail(`${theme} refusal: ran ${JSON.stringify(calls)}`);
		if (failed === before) console.log(`ok   ${theme} refusal`);
		await page.close();
	}

	// An unfinished Start fresh put back at launch: the earlier install didn't start, so the card says it's stopped.
	{
		const failure = {
			message: 'CORSIA put back everything Start fresh had moved. CORSIA couldn’t start your earlier install again, so it isn’t running. Choose Retry to try Start fresh again.',
			details: '`corsia start` exited 1',
			restored: true,
		};
		const page = await open(theme, { start_fresh_preview: preview });
		const before = failed;
		await page.evaluate((given) => window.__corsiaShowStartup({ state: 'start_fresh_failed', failure: given }), failure);
		compare(`${theme} launch undo`, await read(page), { title: 'CORSIA couldn’t start fresh', line: failure.message, details: failure.details, button: 'Retry', quit: true });
		await shot(page, theme, '6-launch-undo');
		await page.click('#retry');
		await page.waitForFunction(() => document.getElementById('retry').textContent === 'Move and start fresh');
		if (failed === before) console.log(`ok   ${theme} launch undo`);
		await page.close();
	}

	// A failure that put everything back offers Retry, which reads the computer again.
	for (const { name, error, line, details, button, next } of [
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
			error: { message: 'CORSIA couldn’t put everything back. Nothing was deleted: what it moved is in ~/CORSIA backups/2026-10-22 13-05.', details: 'could not move ~/Corsia back', restored: false },
			line: 'CORSIA couldn’t put everything back. Nothing was deleted: what it moved is in ~/CORSIA backups/2026-10-22 13-05.',
			details: 'could not move ~/Corsia back',
			button: 'Reopen CORSIA',
			next: 'reopen_app',
		},
	]) {
		const page = await open(theme, { start_fresh_preview: preview, start_fresh: { throws: error } });
		const before = failed;
		await page.evaluate((said) => window.__corsiaShowStartup({ state: 'existing_install', reason: said }), reason);
		await page.click('#retry');
		await page.waitForFunction(() => document.getElementById('retry').textContent === 'Move and start fresh');
		await page.click('#retry');
		await page.waitForFunction(() => document.getElementById('status').textContent === 'CORSIA couldn’t start fresh');
		compare(`${theme} failure ${name}`, await read(page), { line, details, button, list: null, quit: true });
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
