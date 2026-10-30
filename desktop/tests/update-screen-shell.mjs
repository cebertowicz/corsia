// Checks the shell's update page from its actual generated assets (desktop/dist-shell/update.html, update-screen.js and
// update-screen.css): it mounts the app's update screen from the handed-over screen without resetting its percentage or
// stall clock, asks the shell only for recovery starts (automatic once when the wait runs long, a deferred one asked
// again each second as the start it was, manual only on a press), and on Ready returns to the service's page carrying
// the screen. It also opens the page as an app the update reopened does, with the screen native carried, and draws that
// app's own launch answers through the hooks the shell calls. The shell commands are stubbed; this proves the page, not
// the native start.
//
// usage: node desktop/tests/update-screen-shell.mjs [--shell <dist-shell dir>]
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const { chromium } = createRequire(path.join(process.env.PLAYWRIGHT_FROM ?? repo, 'frontend/package.json'))('playwright');
const flag = process.argv.indexOf('--shell');
const shell = flag > -1 ? path.resolve(process.argv[flag + 1]) : path.join(repo, 'desktop/dist-shell');

const NOW = Date.UTC(2026, 9, 24, 21, 0);
let failed = 0;
const fail = (message) => {
	failed += 1;
	console.error(`FAIL ${message}`);
};

// The shell's files, and a stand-in for the service's page at "/" that the Ready answer returns to.
const server = createServer(async (request, response) => {
	const name = new URL(request.url, 'http://127.0.0.1').pathname.slice(1);
	if (name === '') {
		response.writeHead(200, { 'Content-Type': 'text/html' });
		response.end('<!doctype html><title>service</title><p id="service">service page</p>');
		return;
	}
	if (!/^[\w.-]+\.(html|js|css)$/.test(name)) {
		response.writeHead(404);
		response.end();
		return;
	}
	try {
		const type = name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html';
		const file = await readFile(path.join(shell, name));
		response.writeHead(200, { 'Content-Type': type });
		response.end(file);
	} catch {
		response.writeHead(404);
		response.end();
	}
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;

function handoff(overrides = {}) {
	return {
		v: 1, operationId: 'update-1',
		model: { value: 52.4, step: 'upgrading', mode: 'migration', lifted: 0, visitedMigration: true, lastAt: NOW },
		fromVersion: '2026.10.12', toVersion: '2026.10.13', usualSeconds: 45,
		downAt: NOW - 70_000, seqChangedAt: NOW - 20_000, lastSeq: 7, lastPid: 4242,
		failure: null, theme: 'light', route: '/?project=abc',
		...overrides,
	};
}
const fragment = (value) => `corsia-update=${encodeURIComponent(JSON.stringify(value))}`;
const readFragment = (hash) => JSON.parse(decodeURIComponent(hash.replace(/^#corsia-update=/, '')));

const browser = await chromium.launch();

/** Opens the update page with stubbed shell commands; `answers` are the recovery answers, in order. */
async function open(hash, { answers = [], progress = 'advancing', reducedMotion = 'no-preference', readyAfter = Infinity, replay = null } = {}) {
	const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion });
	const page = await context.newPage();
	await page.clock.install({ time: NOW });
	await page.addInitScript(({ answers, progress, origin, readyAfter, replay }) => {
		window.__CORSIA_SHELL__ = { mode: 'local' };
		window.__calls = [];
		if (replay && window.location.pathname === '/index.html') {
			window.addEventListener('DOMContentLoaded', () => {
				if (replay.kind === 'startup') window.__corsiaShowStartup?.(replay.value);
				else window.__corsiaShowServiceFailure?.(replay.value);
			}, { once: true });
		}
		let seq = 7;
		let reads = 0;
		window.__TAURI_INTERNALS__ = { invoke: async (command, args) => {
			window.__calls.push([command, args ?? null]);
			if (command === 'read_startup_progress') {
				if (progress === 'advancing') seq += 1;
				reads += 1;
				if (progress === 'old-then-migration') {
					return reads === 1
						? { schema: 1, pid: 111, seq: 100, phase: 'serving', done: null, total: null }
						: { schema: 1, pid: 4242, seq: reads, phase: 'migrating', done: 1, total: 3 };
				}
				// The shell's reading once the service answers again with no update holding it.
				if (reads > readyAfter) return { ready_url: origin };
				return { schema: 1, pid: 4242, seq, phase: 'migrating', done: 1, total: 4 };
			}
			if (command === 'start_local_service') {
				sessionStorage.setItem('__corsiaStartCalls', String(Number(sessionStorage.getItem('__corsiaStartCalls') ?? '0') + 1));
				const answer = answers.shift() ?? { recovery: 'unavailable' };
				// An answer meant for one kind of start only: any other kind is not this computer's start to run.
				if (answer.onlyFor && answer.onlyFor !== args?.recovery) return { recovery: 'unavailable' };
				return answer.recovery === 'ready' ? { recovery: 'ready', url: origin } : { recovery: answer.recovery };
			}
			return null;
		} };
	}, { answers, progress, origin, readyAfter: Number.isFinite(readyAfter) ? readyAfter : Number.MAX_SAFE_INTEGER, replay });
	await page.goto(`${origin}/update.html#${hash}`, { waitUntil: 'load' });
	await page.clock.runFor(50);
	return page;
}

const starts = (page) => page.evaluate(() => window.__calls.filter(([command]) => command === 'start_local_service').map(([, args]) => args));
const percent = (page) => page.evaluate(() => Number.parseFloat(document.querySelector('.update-screen .percent')?.textContent ?? 'NaN'));
const text = (page) => page.evaluate(() => document.body.textContent ?? '');

try {
	// A stopping service's last serving record must not hide the replacement's migration.
	const replacement = await open(fragment(handoff({
		model: { value: 15, step: 'restarting', mode: 'migration', lifted: 0, visitedMigration: false, lastAt: NOW },
		downAt: NOW, seqChangedAt: null, lastSeq: null, lastPid: null,
	})), { progress: 'old-then-migration' });
	await replacement.clock.runFor(2_000);
	if (!(await text(replacement)).includes('Upgrading your data')) fail('old serving progress hid the replacement migration');
	if ((await percent(replacement)) >= 68) fail('replacement migration left its progress band');

	// Mount and continuity: the same screen, at the same number, step, versions and theme, focused.
	const page = await open(fragment(handoff()));
	const shown = await percent(page);
	if (shown !== 52) fail(`the handed-over 52.4% showed as ${shown}% instead of 52%`);
	const body = await text(page);
	for (const line of ['Updating CORSIA', 'Upgrading your data', '2026.10.12 → 2026.10.13']) if (!body.includes(line)) fail(`the screen lacks "${line}"`);
	const look = await page.evaluate(() => {
		const screen = document.querySelector('.update-screen');
		const probe = document.createElement('i');
		probe.style.background = 'var(--bg-primary)';
		document.body.append(probe);
		const expected = getComputedStyle(probe).backgroundColor;
		probe.remove();
		return { theme: document.documentElement.dataset.theme, background: getComputedStyle(screen).backgroundColor, expected, focused: document.activeElement === screen, transition: getComputedStyle(screen.querySelector('.bar i')).transitionDuration };
	});
	if (look.theme !== 'light') fail(`the page drew the ${look.theme} theme, not the handed-over light one`);
	if (look.background !== look.expected || look.background === 'rgba(0, 0, 0, 0)') fail(`the screen background ${look.background} is not the token ${look.expected}`);
	if (!look.focused) fail('the screen did not take focus');
	if (look.transition !== '0s') fail(`the bar uses a CSS transition lasting ${look.transition}; its width is driven by count-up`);

	// Advancing progress creeps on and never asks for a start.
	await page.clock.runFor(10_000);
	if ((await percent(page)) < shown) fail(`the percentage went back from ${shown}`);
	if ((await starts(page)).length) fail('advancing progress asked for a start');
	// A reload continues from the latest screen.
	const kept = readFragment(await page.evaluate(() => window.location.hash));
	if (kept.model.value < 52.4 || kept.lastSeq <= 7) fail('the address does not carry the latest screen');

	// A record that stops for 60 s runs long: one automatic ask, then the admitted manual offer without an effect.
	const stalled = await open(fragment(handoff({ seqChangedAt: NOW - 55_000 })), { progress: 'frozen', answers: [{ recovery: 'manual_available' }] });
	if ((await starts(stalled)).length) fail('the stalled screen asked before the wait ran long');
	await stalled.clock.runFor(6_000);
	await stalled.clock.runFor(5_000);
	const asked = await starts(stalled);
	if (JSON.stringify(asked) !== JSON.stringify([{ recovery: 'automatic' }])) fail(`a long wait asked ${JSON.stringify(asked)}, not one automatic start`);
	if (!(await text(stalled)).includes('It didn’t start on its own.')) fail('an available manual recovery does not offer Start CORSIA');
	await stalled.getByRole('button', { name: 'Start CORSIA' }).click();
	await stalled.clock.runFor(100);
	const pressed = await starts(stalled);
	if (JSON.stringify(pressed.slice(1)) !== JSON.stringify([{ recovery: 'manual' }])) fail(`the press asked ${JSON.stringify(pressed.slice(1))}, not one manual start`);
	if (!(await text(stalled)).includes('Couldn’t start CORSIA. Quit CORSIA with ⌘Q and open it again.')) fail('a refused press has no quit/reopen exit');
	if (await stalled.getByRole('button', { name: 'Start CORSIA' }).count()) fail('a refused press still offers Start CORSIA');
	await stalled.clock.runFor(5_000);
	if ((await starts(stalled)).length !== 2) fail('a refused press was automatically retried');

	// An automatic refusal never offers an impossible retry.
	const unavailable = await open(fragment(handoff({ seqChangedAt: NOW - 61_000 })), { progress: 'frozen' });
	await unavailable.clock.runFor(6_000);
	if (!(await text(unavailable)).includes('Couldn’t start CORSIA. Quit CORSIA with ⌘Q and open it again.')) fail('an automatic refusal has no quit/reopen exit');
	if (await unavailable.getByRole('button', { name: 'Start CORSIA' }).count()) fail('an automatic refusal still offers Start CORSIA');
	if (JSON.stringify(await starts(unavailable)) !== JSON.stringify([{ recovery: 'automatic' }])) fail('an automatic refusal was retried');

	// Deferred: the waiting line stays and the automatic start is asked again once a second, then Ready returns home.
	const deferred = await open(fragment(handoff({ seqChangedAt: NOW - 61_000 })), {
		progress: 'frozen', answers: [{ recovery: 'deferred' }, { recovery: 'deferred' }, { recovery: 'deferred' }, { recovery: 'ready' }],
	});
	await deferred.clock.runFor(1_100);
	if (!(await text(deferred)).includes('CORSIA is still starting. Nothing you need to do yet.')) fail('a deferred start does not show the waiting line');
	const early = (await starts(deferred)).length;
	await deferred.clock.runFor(1_000);
	const later = (await starts(deferred)).length;
	if (later - early !== 1) fail(`a deferred start was asked ${later - early} times in one second`);
	if ((await starts(deferred)).some((args) => args.recovery !== 'automatic')) fail('a deferred wait asked for anything but the automatic start');
	const back = deferred.waitForURL(`${origin}/?project=abc#corsia-update=*`);
	await deferred.clock.runFor(3_000);
	await back;
	const returned = new URL(deferred.url());
	const carried = readFragment(returned.hash);
	if (carried.operationId !== 'update-1' || carried.model.value < 52.4) fail('Ready returned without the screen');

	// The automatic start is spent; a press that another update defers stays a press over the following seconds, keeps
	// the waiting line, and once nothing holds the lock runs as that press and returns home. Only a manual ask is answered
	// Ready here, so reaching home proves the intent.
	const pressDeferred = await open(fragment(handoff({ seqChangedAt: NOW - 61_000 })), {
		progress: 'frozen',
		answers: [{ recovery: 'manual_available' }, { recovery: 'deferred' }, { recovery: 'deferred' }, { recovery: 'ready', onlyFor: 'manual' }],
	});
	await pressDeferred.clock.runFor(1_100);
	await pressDeferred.getByRole('button', { name: 'Start CORSIA' }).click();
	await pressDeferred.clock.runFor(100);
	// Three readings across the next seconds: the press is asked again on the screen's next recompute a second or more
	// after the last ask, and stays deferred meanwhile.
	for (const reading of [1, 2, 3]) {
		const body = await text(pressDeferred);
		if (!body.includes('CORSIA is still starting. Nothing you need to do yet.')) fail(`a deferred press does not keep the waiting line (reading ${reading})`);
		if (body.includes('It still didn’t start.') || body.includes('It didn’t start on its own.')) fail(`a deferred press was shown as a failed start (reading ${reading})`);
		if (reading < 3) await pressDeferred.clock.runFor(1_000);
	}
	const deferredAsks = (await starts(pressDeferred)).map(({ recovery }) => recovery);
	if (JSON.stringify(deferredAsks) !== JSON.stringify(['automatic', 'manual', 'manual'])) fail(`a deferred press was asked again as ${JSON.stringify(deferredAsks)}`);
	const pressedHome = pressDeferred.waitForURL((url) => url.pathname === '/' && url.hash.startsWith('#corsia-update='), { timeout: 5_000 });
	await pressDeferred.clock.runFor(2_000);
	await pressedHome.catch(() => fail('the deferred press did not run as a press once the lock was free'));

	// The service comes back by itself after a manual offer: the next reading returns home, asking nothing.
	const late = await open(fragment(handoff({ seqChangedAt: NOW - 61_000 })), { progress: 'frozen', readyAfter: 3, answers: [{ recovery: 'manual_available' }] });
	await late.clock.runFor(1_100);
	if (!(await text(late)).includes('It didn’t start on its own.')) fail('the late-recovery case did not first offer Start CORSIA');
	// Every start this page will ask is in by now; the service page that follows keeps its own list.
	const lateStarts = await starts(late);
	const home = late.waitForURL(`${origin}/?project=abc#corsia-update=*`);
	await late.clock.runFor(10_000);
	await home;
	if (readFragment(new URL(late.url()).hash).operationId !== 'update-1') fail('the late return lost the screen');
	if ((await late.evaluate(() => window.__calls.length)) !== 0) fail('the service page asked the shell for anything on arrival');
	if (JSON.stringify(lateStarts) !== JSON.stringify([{ recovery: 'automatic' }])) fail(`the late return asked ${JSON.stringify(lateStarts)}, not only the one automatic start`);

	// An app the update reopened opens here with the screen native carried on the command line. A non-Ready answer is
	// retained by native ShellState, then this generated hook returns to index.html; its normal page-load hook draws the
	// existing startup guidance without asking for another start.
	for (const [name, replay, expected] of [
		['approval', { kind: 'startup', value: { state: 'requires_approval' } }, 'Keep CORSIA running'],
		['disk image', { kind: 'startup', value: { state: 'disk_image' } }, 'Move CORSIA to Applications'],
		['launch failure', { kind: 'failure', value: 'could not finish startup' }, 'CORSIA couldn’t start'],
	]) {
		const relaunched = await open(fragment(handoff({ route: "/?q=(it's)!*~", seqChangedAt: NOW - 5_000 })), { replay });
		if (!relaunched.url().includes('/update.html#corsia-update=')) fail(`the ${name} page left for ${relaunched.url()} before its answer`);
		if ((await percent(relaunched)) !== 52) fail(`the ${name} page did not draw the carried whole-number percentage before its answer`);
		if (replay.kind === 'startup') await relaunched.evaluate((startup) => window.__corsiaShowStartup?.(startup), replay.value);
		else await relaunched.evaluate((reason) => window.__corsiaShowServiceFailure?.(reason), replay.value);
		await relaunched.waitForURL(`${origin}/index.html`, { timeout: 5_000 }).catch(() => fail(`the ${name} hook did not return to the startup page`));
		await relaunched.waitForFunction((line) => document.body.innerText.includes(line), expected, { timeout: 5_000 }).catch(() => fail(`the startup page did not render ${name} guidance`));
		if (Number(await relaunched.evaluate(() => sessionStorage.getItem('__corsiaStartCalls') ?? '0')) !== 0) fail(`the ${name} guidance started CORSIA again`);
	}

	// Ready retains the original handoff and returns to the service page as before.
	const carriedRoute = "/?q=(it's)!*~";
	const readyLaunch = await open(fragment(handoff({ route: carriedRoute, seqChangedAt: NOW - 5_000 })));
	const launchHome = readyLaunch.waitForURL((url) => url.pathname === '/' && url.hash.startsWith('#corsia-update='), { timeout: 5_000 });
	await readyLaunch.evaluate((o) => window.__corsiaShowStartup?.({ state: 'ready', url: `${o}/#corsia-update=older` }), origin);
	await readyLaunch.clock.runFor(100);
	await launchHome.catch(() => fail('a Ready launch answer did not return to the service page'));
	const landed = new URL(readyLaunch.url());
	if (landed.search !== new URL(carriedRoute, origin).search) fail(`a Ready launch answer returned to ${landed.search}, not the kept page`);
	if (readFragment(landed.hash).operationId !== 'update-1' || readFragment(landed.hash).model.value < 52.4) fail('a Ready launch answer returned without the latest screen');

	// Reduced motion keeps the bar still.
	const still = await open(fragment(handoff()), { reducedMotion: 'reduce' });
	const stillBar = await still.evaluate(() => getComputedStyle(document.querySelector('.update-screen .bar i')).transitionDuration);
	if (Number.parseFloat(stillBar) > 0.00001) fail(`the bar still transitions for ${stillBar} with reduced motion`);

	// No handoff, a forged one, or one carrying an action: no screen, no start; the window goes to the startup page.
	for (const hash of ['', 'corsia-update=%7B%22v%22%3A1%7D', fragment({ ...handoff(), route: 'https://evil.example/' })]) {
		const rejected = await open(hash);
		await rejected.clock.runFor(2_000);
		if (!rejected.url().endsWith('/index.html')) fail(`"${hash.slice(0, 40)}" stayed on ${rejected.url()}`);
		if ((await starts(rejected)).length) fail(`"${hash.slice(0, 40)}" asked for a start`);
	}
} finally {
	await browser.close();
	server.close();
}

if (failed) process.exit(1);
console.log('ok: the shell update page continues the handed-over screen and asks only for recovery starts');
