// Renders the cloud-first setup frames (shared/onboarding/2026-10-16-cloud-first-setup-frames.md) from the real app.
// The page is the app's own root route and SetupScreen: only the `/api/*` answers are mocked (Playwright routes), and
// every state is reached by clicking and typing as a person would. No DOM state is set. The disposable frontend
// (scripts/setup-frames/dev.sh, port 5191, backend refused) serves it; HOME and the packaged app are never touched.
// Usage: node scripts/setup-frames/cloud-first.mjs <outDir> <metricsFile> [--base <url on port 5191>]
// Each frame must show its state-specific text, stay 8 px clear of the dock, and not scroll, or the run exits 1.
import { chromium } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const [outDir, metricsFile] = args;
const flag = (n) => (args.includes(n) ? args[args.indexOf(n) + 1] : undefined);
const base = flag('--base') ?? 'http://127.0.0.1:5191';
if (!['5191', '5217', '3217'].includes(new URL(base).port)) throw new Error(`refusing ${base}`);
if (!outDir || !metricsFile) throw new Error('usage: cloud-first.mjs <outDir> <metricsFile> [--base url]');
fs.mkdirSync(outDir, { recursive: true });

const D = { w: 1280, h: 900 };
const F = { w: 800, h: 600 };
const ASK = 'Which of these do you pay for?';
const SWITCH = 'Also run a local model on this computer';
const LOW_MEMORY = 'This computer doesn’t have enough memory to run a local model, so pick at least one above.';

const PROVIDERS = [
	{ name: 'anthropic', label: 'Anthropic', type: 'cloud', has_key: false, masked_key: null, api_base: null, docs_url: null, models: [] },
	{ name: 'openai', label: 'OpenAI', type: 'cloud', has_key: false, masked_key: null, api_base: null, docs_url: null, models: [] },
];
const STARTER = { name: 'Ternary Bonsai 2', repository: 'prism-ml/Ternary-Bonsai-2-27B-mlx-2bit', download_size_bytes: 8_608_739_065, download_size_gb: 8.6, context_window: 32768 };
const STARTER_NOTE = "oMLX didn’t recommend a model for this computer, so CORSIA chose Ternary Bonsai 2.";
const MODEL = { name: 'Qwen 3.8', repository: 'example/qwen', download_size_bytes: 30_000_000_000, download_size_gb: 30, context_window: 65536 };

/** What the mocked backend answers. Each frame starts from `fresh()` and the driver changes it as the story moves. */
function fresh(over = {}) {
	return {
		status: { apple_silicon: true, macos_supported: true, local_eligible: true, free_disk_gb: 500, work_folder: '~/CORSIA', ...over.status },
		pick: over.pick ?? { state: 'picked', model: MODEL, message: null },
		downloading: false,
		// Their own oMLX under management had no pick, so its source row carries the starter note (the server's `omlx_management`).
		ownFallback: over.ownFallback ?? false,
		claude: over.claude ?? 'not_installed', // not_installed | needs_auth | ready
		codex: over.codex ?? 'not_installed',
		install: 'running', // running | done | failed | cancelled
		installReason: null,
		signin: 'waiting',
		statusDelayMs: 0,
	};
}

function sourcesOf(world) {
	const out = [];
	if (world.status.local_eligible !== false) {
		const managed = world.ownFallback ? { omlx_management: { state: 'managed', cannot_check: null, required_version: '0.7.1rc1', restores: [], forgets_key: true, starter_note: STARTER_NOTE, starter_name: STARTER.name } } : {};
		out.push(world.localReady ? { source: 'omlx', name: 'Local model', state: 'ready', download_size: '30 GB' } : world.downloading
			? { source: 'omlx', name: 'Local model', state: 'needs_setup', setup_stage: 'Downloading', setup_downloading: true, download_percent: 38, download_eta_seconds: 840, download_size: '30 GB', ...managed }
			: { source: 'omlx', name: 'Local model', state: 'needs_setup', download_size: '30 GB' });
	}
	for (const [source, name, key] of [['claude-code', 'Claude Code', 'claude'], ['codex', 'Codex', 'codex']]) {
		const state = world[key];
		out.push({ source, name, state, accounts: state === 'ready' ? [{ state: 'ready' }] : [] });
	}
	return out;
}

async function mock(context, world) {
	await context.route((url) => url.pathname.startsWith('/api/'), async (route) => {
		const req = route.request();
		const url = new URL(req.url());
		const p = url.pathname;
		const method = req.method();
		const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
		if (p === '/api/auth/status') return json({ has_users: false });
		if (p === '/api/setup/status') {
			if (world.statusDelayMs) await new Promise((r) => setTimeout(r, world.statusDelayMs));
			return json(world.status);
		}
		if (p === '/api/setup/local-model/stop') return json({ state: 'not_checked', model: null, message: null });
		if (p === '/api/setup/local-model') return json(world.pick);
		if (p === '/api/setup/local-download') { world.downloading = true; return json({ in_progress: true }); }
		if (p === '/api/setup/first-account') return json({ token: 'frames-token', user: { id: 'setup-user', name: 'First Person' } });
		if (p === '/api/providers/sources' || p === '/api/providers/sources/recheck') return json({ sources: sourcesOf(world) });
		if (p === '/api/providers/local/setup') { world.downloading = true; return json({ ok: true, in_progress: true }); }
		const cli = p.match(/^\/api\/providers\/cli\/([\w-]+)\/install$/);
		if (cli) {
			if (method === 'POST') return json({ ok: true });
			if (method === 'DELETE') { world.install = 'cancelled'; return json({ state: 'cancelled', reason: null, path_shadowed_by: null }); }
			return json({ state: world.install, reason: world.installReason, path_shadowed_by: null });
		}
		if (p === '/api/corsia-agent') return json({ thread_id: 'conductor-1' });
		if (p === '/api/vendor-accounts' && method === 'GET') return json({ accounts: [] });
		if (p === '/api/vendor-accounts') return json({ id: 'acct-1' });
		if (p === '/api/vendor-accounts/acct-1/signin') return json({ account_id: 'acct-1', status: 'waiting' });
		if (p === '/api/vendor-accounts/acct-1/signin/check') return json({ account_id: 'acct-1', status: world.signin });
		if (p === '/api/settings/providers') return json({ providers: PROVIDERS });
		return json({}, 404);
	});
}

// ---- the drivers: what a person does, in the real UI ---------------------------------------------------------

const settle = (page, ms = 350) => page.waitForTimeout(ms);
// check() clicks and then requires the box to read checked, so a click the page swallowed fails the frame instead of drawing the wrong state.
const pick = async (page, label) => { await page.getByRole('checkbox', { name: new RegExp(`^${label}`) }).check({ timeout: 5000 }); await settle(page); };
const getStarted = async (page) => { await page.getByRole('button', { name: 'Get started' }).click(); await settle(page, 500); };
async function createAccount(page, label = 'Continue') {
	await page.locator('input[autocomplete="name"]').fill('First Person');
	await page.locator('input[autocomplete="username"]').fill('first');
	for (const input of await page.locator('input[autocomplete="new-password"]').all()) await input.fill('correct-password');
	await settle(page, 200);
	await page.getByRole('button', { name: label, exact: true }).click();
	await settle(page, 700);
}
/** Open Welcome and wait for the question (or the ready line) to be there. */
async function welcome(page, hold = 'Which of these') {
	await page.goto(`${base}/`, { waitUntil: 'networkidle' });
	await page.getByText(hold).first().waitFor({ timeout: 10000 });
	await settle(page, 400);
}
/** Welcome with Claude checked, through the account, onto the AI step. */
async function toAiStep(page, picks) {
	await welcome(page);
	for (const label of picks) await pick(page, label);
	await getStarted(page);
	await createAccount(page);
}

/** The pixels of a page region as the screenshot shows them, mask and fade included, so a check sees what a person sees. */
async function pixels(page, rect) {
	const clip = { x: Math.max(0, Math.floor(rect.left)), y: Math.max(0, Math.floor(rect.top)), width: Math.max(1, Math.ceil(rect.right - rect.left)), height: Math.max(1, Math.ceil(rect.bottom - rect.top)) };
	const png = (await page.screenshot({ clip })).toString('base64');
	return page.evaluate(async (b64) => {
		const img = new Image();
		img.src = `data:image/png;base64,${b64}`;
		await img.decode();
		const canvas = new OffscreenCanvas(img.width, img.height);
		const g = canvas.getContext('2d');
		g.drawImage(img, 0, 0);
		return { w: img.width, h: img.height, data: Array.from(g.getImageData(0, 0, img.width, img.height).data) };
	}, png);
}
/** How far a pixel is from the background, summed over its channels (0 to 765). */
const away = (data, i, bg) => Math.abs(data[i] - bg[0]) + Math.abs(data[i + 1] - bg[1]) + Math.abs(data[i + 2] - bg[2]);
const rgb = (css) => css.match(/[\d.]+/g).slice(0, 3).map(Number);
/** The strongest ink in each pixel row of a region, top to bottom. */
const rowInk = ({ w, h, data }, bg) => Array.from({ length: h }, (_, y) => Math.max(...Array.from({ length: w }, (_, x) => away(data, (y * w + x) * 4, bg))));
/** What CORSIA changes at full colour: its pixels are the same with the body's edge cue taken off, and its ink is its own colour's contrast. */
async function termsAtFullColour(page, g, bg) {
	const bad = [];
	const text = await pixels(page, g.glyphs);
	const full = rgb(g.termsColor).reduce((sum, c, i) => sum + Math.abs(c - bg[i]), 0);
	const strongest = Math.max(...rowInk(text, bg));
	if (strongest < full * 0.7) bad.push(`What CORSIA changes is dimmed: its strongest ink is ${strongest} of the ${full} its colour has`);
	await page.evaluate(() => { document.querySelector('.body').style.maskImage = 'none'; });
	const plain = await pixels(page, g.glyphs);
	await page.evaluate(() => { document.querySelector('.body').style.maskImage = ''; });
	const dimmed = Math.max(...text.data.map((v, i) => Math.abs(v - plain.data[i])));
	if (dimmed > 8) bad.push(`the edge cue dims What CORSIA changes: its pixels change by up to ${dimmed} with the cue taken off`);
	return bad;
}
const page_ = {
	/** Where things sit, as the page lays them out. */
	geometry: (page) => page.evaluate(() => {
		const box = (e) => { if (!e) return null; const r = e.getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom }; };
		const body = document.querySelector('.body');
		const terms = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'What CORSIA changes');
		let glyphs = null;
		if (terms) {
			const walker = document.createTreeWalker(terms, NodeFilter.SHOW_TEXT);
			let text = walker.nextNode();
			while (text && text.textContent.trim() !== 'What CORSIA changes') text = walker.nextNode();
			const range = document.createRange(); range.selectNodeContents(text);
			glyphs = box(range);
		}
		const first = document.querySelector('[role="radio"]');
		return {
			bg: getComputedStyle(document.querySelector('.setup-shell')).backgroundColor,
			body: box(body), dock: box(document.querySelector('.pane > footer')), found: box([...document.querySelectorAll('.spec')].find((e) => e.textContent.includes('CORSIA found oMLX'))),
			terms: box(terms), glyphs, termsColor: terms ? getComputedStyle(terms).color : null,
			choice: box(first), choiceLabel: box(first?.querySelector('.lbl')), labelColor: first ? getComputedStyle(first.querySelector('.lbl')).color : null,
			more: body.dataset.more, above: body.dataset.above, scrollTop: body.scrollTop,
		};
	}),
};

// ---- the frames ------------------------------------------------------------------------------------------------
// frame id -> { setup(world), drive(page, world), want: strings on screen, no: strings that must not be }.
const FRAMES = {
	'q-empty': { want: [ASK, 'Choose any that apply. CORSIA sets them up after you create your account.', 'A pay-as-you-go API key', SWITCH, 'On until you pick one above.', 'Get started'], drive: (page) => welcome(page) },
	'q-claude': {
		check: async (page, world) => {
			const bad = [];
			if (!world.wash) return ['the hover check did not run'];
			if (world.wash.hover === 'rgba(0, 0, 0, 0)') bad.push('a checked row has no wash under the pointer');
			if (world.wash.hover !== world.wash.rest) bad.push(`a checked row changes colour under the pointer (${world.wash.hover} against ${world.wash.rest} at rest)`);
			return bad;
		}, want: ['CORSIA installs Claude Code, made by Anthropic, and you sign in.', SWITCH], no: ['On until you pick one above.'],
		drive: async (page, world) => {
			await welcome(page); await pick(page, 'Claude');
			const wash = () => page.getByRole('checkbox', { name: /^Claude/ }).evaluate((node) => getComputedStyle(node).backgroundColor);
			world.wash = { hover: await wash() };
			await page.mouse.move(0, 0); await settle(page, 400);
			world.wash.rest = await wash();
		} },
	'q-both': { want: ['CORSIA installs Codex, made by OpenAI, and you sign in.', 'CORSIA installs Claude Code, made by Anthropic, and you sign in.'], drive: async (page) => { await welcome(page); await pick(page, 'Claude'); await pick(page, 'ChatGPT'); } },
	'q-key': { want: ['From Anthropic, OpenAI or another provider. You paste it after you create your account.', SWITCH], no: ['On until you pick one above.'], drive: async (page) => { await welcome(page); await pick(page, 'A pay-as-you-go API key'); } },
	'q-all': { want: ['CORSIA installs Claude Code, made by Anthropic, and you sign in.', 'CORSIA installs Codex, made by OpenAI, and you sign in.', 'From Anthropic, OpenAI or another provider. You paste it after you create your account.', SWITCH], no: ['On until you pick one above.'], drive: async (page) => { await welcome(page); for (const name of ['Claude', 'ChatGPT', 'A pay-as-you-go API key']) await pick(page, name); } },
	'q-switch-off': { want: ['CORSIA installs Claude Code, made by Anthropic, and you sign in.', SWITCH], no: ['The download starts in the background', 'On until you pick one above.'], drive: async (page) => { await welcome(page); for (const name of ['Claude', 'ChatGPT', 'A pay-as-you-go API key']) await pick(page, name); await page.getByRole('button', { name: SWITCH }).click(); await settle(page); } },
	'q-ready': { setup: { status: { ready_ai: 'Claude Code' } }, want: ['Claude Code is ready. CORSIA uses it for chat.', SWITCH], no: [ASK], drive: (page) => welcome(page, 'is ready') },
	'q-checking': { setup: { status: { ai_check_pending: true } }, want: ['Checking for Claude Code and Codex…', SWITCH], no: [ASK], drive: async (page) => { await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' }); await page.getByText('Checking for Claude Code').waitFor({ timeout: 10000 }); await settle(page, 300); } },
	'q-no-local': { setup: { status: { local_eligible: false }, pick: { state: 'low_memory', model: null, message: null } }, want: [ASK, LOW_MEMORY, 'Get started'], no: [SWITCH], disabled: ['Get started'], drive: (page) => welcome(page) },
	'q-no-local-claude': { setup: { status: { local_eligible: false }, pick: { state: 'low_memory', model: null, message: null } }, want: [ASK, 'CORSIA installs Claude Code, made by Anthropic, and you sign in.'], no: [LOW_MEMORY, SWITCH], drive: async (page) => { await welcome(page); await pick(page, 'Claude'); } },
	'q-own-omlx': {
		// Measured on the pixels the screenshot holds, not on styles: the dock-edge fade must leave What CORSIA changes at full colour and
		// must show the top of the first choice above the dock, so the preselected "Let CORSIA manage it" is never out of sight.
		check: async (page) => {
			const bad = [];
			const floor = page.viewportSize().height <= 600;
			const g = await page_.geometry(page);
			if (!g.terms || !g.choice) return ['What CORSIA changes or the first choice is not on screen'];
			const bg = rgb(g.bg);
			// One control holds the terms at every window size: no second label, and no paragraph of terms in the page.
			const labelled = await page.evaluate(() => ({ terms: [...document.querySelectorAll('button')].filter((b) => b.textContent.trim() === 'What CORSIA changes').length, details: [...document.querySelectorAll('button')].filter((b) => b.textContent.trim() === 'Details').length, paragraph: document.querySelectorAll('.disclose').length }));
			if (labelled.terms !== 1) bad.push(`${labelled.terms} What CORSIA changes controls (needs exactly one)`);
			if (labelled.details || labelled.paragraph) bad.push('the old Details label or the terms paragraph is still in the page');
			// The control is one collapsed link under the choices at every size: below the last choice, never beside the found line.
			const lastChoice = await page.evaluate(() => Math.max(...[...document.querySelectorAll('[role="radio"]')].map((n) => n.getBoundingClientRect().bottom)));
			if (g.terms.top < lastChoice - 1) bad.push(`What CORSIA changes sits at ${Math.round(g.terms.top)}, not under the last choice (ends ${Math.round(lastChoice)})`);
			// At the floor it sits under the dock until the list is scrolled (w-own-omlx-scrolled proves it there); where it is on screen, it is at full colour.
			if (!floor || g.terms.bottom <= g.dock.top) bad.push(...await termsAtFullColour(page, g, bg));
			if (!floor) return bad;
			// The first choice peeks: at least 24 px of it above the dock, with its label's ink on screen.
			const room = g.dock.top - g.choice.top;
			if (room < 24) bad.push(`the first choice shows ${Math.round(room)} px above the dock (needs 24)`);
			const label = { ...g.choiceLabel, bottom: Math.min(g.choiceLabel.bottom, g.dock.top) };
			if (label.bottom - label.top >= 2) {
				const ink = await pixels(page, label);
				const lit = ink.data.filter((_, i) => i % 4 === 0 && away(ink.data, i, bg) > 120).length;
				if (lit < 15) bad.push(`the first choice's label is not visible above the dock (${lit} inked pixels)`);
			} else bad.push('the first choice\'s label is under the dock');
			if (g.more !== 'true') bad.push(`no cue at the dock edge while content sits under it (data-more ${g.more})`);
			return bad;
		},
		scrolls: true, setup: { status: { existing_omlx: true }, pick: { state: 'own_omlx', model: null, message: null } }, want: [ASK, 'CORSIA found oMLX on this computer. Both choices use it.', 'Let CORSIA manage it', 'Recommended. CORSIA sets up oMLX for the model it installs. You can put your own settings back in Settings any time.', 'Use mine', 'CORSIA uses your models as they are and changes nothing in oMLX.', 'What CORSIA changes'], no: ['Details', 'Letting CORSIA manage it'], drive: (page) => welcome(page) },
	'q-own-omlx-scrolled': {
		scrolls: true, setup: { status: { existing_omlx: true }, pick: { state: 'own_omlx', model: null, message: null } }, want: ['Let CORSIA manage it', 'Use mine'], no: ['Letting CORSIA manage it'],
		// The scrolled end: nothing under the dock, and under the pinned title and intro the list fades out instead of
		// hard-clipping a half line, measured on the pixels along the body's top edge.
		check: async (page) => {
			const bad = [];
			const g = await page_.geometry(page);
			const bg = rgb(g.bg);
			if (g.choice.bottom > g.dock.top) bad.push(`a choice ends at ${Math.round(g.choice.bottom)}, under the dock that starts at ${Math.round(g.dock.top)}`);
			const last = await page.evaluate(() => Math.max(...[...document.querySelectorAll('[role="radio"]')].map((n) => n.getBoundingClientRect().bottom)));
			if (last > g.dock.top) bad.push(`the last choice ends at ${Math.round(last)}, under the dock`);
			// Scrolled to the end, the collapsed link under the choices is on screen, below them and at full colour.
			if (!g.terms) bad.push('What CORSIA changes is not on screen');
			else {
				if (g.terms.top < last - 1) bad.push(`What CORSIA changes sits at ${Math.round(g.terms.top)}, not under the last choice (ends ${Math.round(last)})`);
				if (g.terms.bottom > g.dock.top) bad.push(`What CORSIA changes ends at ${Math.round(g.terms.bottom)}, under the dock that starts at ${Math.round(g.dock.top)}`);
				else bad.push(...await termsAtFullColour(page, g, bg));
			}
			if (g.more !== 'false') bad.push('the body is not scrolled to its end');
			if (g.above !== 'true') bad.push(`no cue under the intro while the list is scrolled (data-above ${g.above})`);
			const edge = await pixels(page, { left: g.body.left, right: g.body.right, top: g.body.top, bottom: g.body.top + 2 });
			const clipped = Math.max(...rowInk(edge, bg));
			const text = rgb(g.labelColor).reduce((sum, c, i) => sum + Math.abs(c - bg[i]), 0);
			if (clipped > text * 0.3) bad.push(`ink along the body's top edge is ${clipped}, a hard clip: the list should fade out there (full text is ${text})`);
			return bad;
		},
		drive: async (page) => {
			await welcome(page);
			await page.mouse.move(500, 250);
			await page.mouse.wheel(0, 3000);
			await settle(page, 600);
		},
	},
	'q-terms-open': {
		floating: true, setup: { status: { existing_omlx: true }, pick: { state: 'own_omlx', model: null, message: null } }, want: ['Letting CORSIA manage it'], scrolls: true,
		check: async (page) => {
			const bad = [];
			const m = await page.evaluate(() => {
				const button = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'What CORSIA changes');
				const pop = [...document.querySelectorAll('p')].find((p) => p.textContent.includes('Letting CORSIA manage it'));
				if (!button || !pop) return null;
				const b = button.getBoundingClientRect(); const p = pop.getBoundingClientRect();
				const card = pop.closest('[data-popover]');
				const cr = card.getBoundingClientRect();
				const cut = [];
				const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
				for (let node = walker.nextNode(); node; node = walker.nextNode()) {
					if (!node.textContent.trim() || card.contains(node)) continue;
					const range = document.createRange(); range.selectNodeContents(node);
					for (const line of range.getClientRects()) {
						if (line.height < 4 || line.right <= cr.left || line.left >= cr.right) continue;
						// A line is cut when the card covers part of it: partly down it, or only the first or last words along it.
						const overlap = Math.min(line.bottom, cr.bottom) - Math.max(line.top, cr.top);
						const along = Math.min(line.right, cr.right) - Math.max(line.left, cr.left);
						if (overlap > 1 && along > 1 && (overlap < line.height - 1 || along < line.width - 1)) cut.push(node.textContent.trim().slice(0, 40));
					}
				}
				return { cut, buttonTop: b.top, buttonBottom: b.bottom, popTop: cr.top, popBottom: cr.bottom, vh: innerHeight, vw: innerWidth, popLeft: cr.left, popRight: cr.right };
			});
			if (!m) return ['What CORSIA changes or its popover is not on screen'];
			// It opens from the control under the choices: just above it or just below it, and wholly inside the window.
			const below = m.popTop - m.buttonBottom, above = m.buttonTop - m.popBottom;
			if (!((below >= -1 && below <= 24) || (above >= -1 && above <= 24))) bad.push(`the popover is not against What CORSIA changes (control ${Math.round(m.buttonTop)} to ${Math.round(m.buttonBottom)}, popover ${Math.round(m.popTop)} to ${Math.round(m.popBottom)})`);
			if (m.popTop < 0 || m.popBottom > m.vh || m.popLeft < 0 || m.popRight > m.vw) bad.push(`the popover is not fully in the window (${Math.round(m.popLeft)},${Math.round(m.popTop)} to ${Math.round(m.popRight)},${Math.round(m.popBottom)} of ${m.vw}x${m.vh})`);
			// And no line of text anywhere is left half under its edge: each is wholly covered or wholly clear.
			for (const cut of m.cut) bad.push(`the popover cuts through the line "${cut}"`);
			return bad;
		},
		drive: async (page) => {
			await welcome(page);
			await page.getByRole('button', { name: 'What CORSIA changes', exact: true }).click();
			await settle(page, 600);
		},
	},
	'p-local-row-rest': {
		want: ['Installing Claude Code', 'This computer', 'Free and private, and works offline. 30 GB download.', 'Download'], no: ['Local model', 'didn’t recommend', 'Downloading in the background'],
		// The local switch is off, so nothing downloads: the This computer row rests on its new line, with Download as its action.
		drive: async (page) => {
			await welcome(page); await pick(page, 'Claude');
			await page.getByRole('button', { name: SWITCH }).click(); await settle(page);
			await getStarted(page); await createAccount(page);
		},
	},
	'p-starter-note': {
		setup: { pick: { state: 'picked', model: STARTER, message: STARTER_NOTE } },
		want: ['Installing Claude Code', 'This computer', 'Downloading in the background', STARTER_NOTE], no: ['Local model'],
		// oMLX recommended nothing, so CORSIA chose the starter model and says so under the This computer row while it downloads.
		drive: (page) => toAiStep(page, ['Claude']),
	},
	'p-own-omlx-note': {
		setup: { status: { existing_omlx: true }, pick: { state: 'own_omlx', model: null, message: null }, ownFallback: true },
		want: ['Installing Claude Code', 'This computer', 'Downloading in the background', STARTER_NOTE], no: ['Use your oMLX?'],
		// Their own oMLX, managed by CORSIA, recommended nothing: the same note sits under the same This computer row as p-starter-note,
		// reached by Welcome (own oMLX, Let CORSIA manage it), Use it, then the account. The background job takes the starter's name from
		// the management view, so the frame is pixel-identical to p-starter-note (cloud-first-pixdiff.py fails if any pixel differs).
		drive: async (page) => {
			await welcome(page); await pick(page, 'Claude'); await getStarted(page);
			await page.getByRole('button', { name: 'Use it', exact: true }).click(); await settle(page, 700);
			await createAccount(page);
		},
	},
	'ac-account': { want: ['Create your account', 'Work folder', 'Continue'], drive: async (page) => { await welcome(page); await pick(page, 'Claude'); await getStarted(page); } },
	'p-installing': { setup: { claude: 'not_installed' }, want: ['Installing Claude Code', 'Setting up what you chose. You can add the others with CORSIA later.'], no: ['Set up', 'Codex', 'Installing with', 'up to 3 minutes'], drive: (page) => toAiStep(page, ['Claude']) },
	'p-signing-in': { want: ['Finish signing in to your Claude account in the browser window that just opened.', 'Sign in'], no: ['Open sign-in again', 'Codex'], drive: async (page, world) => { await toAiStep(page, ['Claude']); world.install = 'done'; world.claude = 'needs_auth'; await settle(page, 4000); } },
	'p-ready': { want: ['Start using CORSIA', 'Uses your Claude subscription.', 'Ready'], no: ['Codex'], drive: async (page, world) => { await toAiStep(page, ['Claude']); world.install = 'done'; world.claude = 'needs_auth'; await settle(page, 4000); world.signin = 'completed'; world.claude = 'ready'; await settle(page, 3600); } },
	'p-cancelled': {
		want: ['Claude Code', 'Set up', 'CORSIA installs Claude Code for you.', 'Ready when the download finishes in about 14 min.'], no: ['Installing', 'Codex', 'signed in'],
		drive: async (page) => { await toAiStep(page, ['Claude']); await page.locator('section[aria-label="Claude Code"]').getByRole('button', { name: 'Cancel', exact: true }).click(); await settle(page, 1800); },
	},
	'p-late-ready': {
		want: ['Installing Codex', 'Uses your Claude subscription.', 'Ready'], no: ['Installing Claude Code', 'Set up'],
		// The check had not finished when Claude was checked; by the AI step the tool is found ready, so its row says so
		// while Codex, still missing, installs beside it.
		drive: async (page, world) => {
			await welcome(page); await pick(page, 'Claude'); await pick(page, 'ChatGPT'); await getStarted(page);
			world.claude = 'ready';
			await createAccount(page);
			await settle(page, 1200);
		},
	},
	'q-slow-check': {
		setup: { status: { ai_check_pending: true } }, want: ['Checking for Claude Code and Codex…', SWITCH], no: [ASK, 'On until you pick one above.'], disabled: ['Get started'],
		drive: async (page) => { await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' }); await page.getByText('Checking for Claude Code').waitFor({ timeout: 10000 }); await settle(page, 800); },
	},
	'p-both-installing': { want: ['Installing Claude Code', 'Installing Codex'], no: ['Set up'], drive: (page) => toAiStep(page, ['Claude', 'ChatGPT']) },
	'p-key': { want: ['API key', 'Save', 'Ready when you save your key or the download finishes in about 14 min.'], no: ['Set up', 'Use an API key instead', 'Claude Code', 'Codex'], select: 'Anthropic', focused: 'input[aria-label="API key"]', drive: (page) => toAiStep(page, ['A pay-as-you-go API key']) },
	'p-key-and-claude': { want: ['API key', 'Installing Claude Code', 'Ready when Claude Code is signed in, you save your key or the download finishes in about 14 min.'], no: ['Codex'], select: 'Anthropic', drive: (page) => toAiStep(page, ['A pay-as-you-go API key', 'Claude']) },
	'p-nothing': { want: ['CORSIA needs one to start. You can add the others with CORSIA later.', 'Claude Code', 'Codex', 'Set up'], no: ['API key', 'Paste your key'], drive: (page) => toAiStep(page, []) },
	'p-no-local-key': { setup: { status: { local_eligible: false }, pick: { state: 'low_memory', model: null, message: null } }, want: ['API key', 'Save', 'Save your API key to continue.'], no: ['This computer', 'Connect Claude Code', 'Codex'], select: 'Anthropic', drive: (page) => toAiStep(page, ['A pay-as-you-go API key']) },
	'p-local-fallback': {
		setup: {},
		want: ['Couldn’t install Claude Code', 'Start using CORSIA', 'Chat runs on the local model for now.'],
		no: ['Or start on the local model now', 'until Claude Code is ready'],
		fix: ['Try again', 'Check your connection, then choose Try again.', 'Open the install page'],
		drive: async (page, world) => {
			await welcome(page); await pick(page, 'Claude'); await getStarted(page);
			world.install = 'failed'; world.installReason = 'no internet connection';
			world.localReady = true;
			await createAccount(page);
			await settle(page, 4000);
		},
	},
};

const only = flag('--only')?.split(',');
const frames = [];
const both = (file, frame, size) => {
	frames.push({ file, frame, ...size, theme: 'light' });
	frames.push({ file: `${file}-dark`, frame, ...size, theme: 'dark' });
};
// Storyboards at the default window. C2: pays for Claude. C5: only an API key.
both('c2-1-welcome-question-1280x900', 'q-empty', D);
both('c2-2-welcome-claude-1280x900', 'q-claude', D);
both('c2-3-account-1280x900', 'ac-account', D);
both('c2-4-installing-1280x900', 'p-installing', D);
both('c2-5-signing-in-1280x900', 'p-signing-in', D);
both('c2-6-ready-1280x900', 'p-ready', D);
both('c5-1-welcome-key-1280x900', 'q-key', D);
both('c5-2-key-step-1280x900', 'p-key', D);
both('c5-3-nothing-checked-step-1280x900', 'p-nothing', D);
both('c1-3-all-checked-switch-off-1280x900', 'q-switch-off', D);
both('w-own-omlx-1280x900', 'q-own-omlx', D);
both('w-terms-open-1280x900', 'q-terms-open', D);
// Welcome states at the floor.
const WELCOME_FLOOR = [['w-empty', 'q-empty'], ['w-claude', 'q-claude'], ['w-both', 'q-both'], ['w-all', 'q-all'], ['w-key', 'q-key'], ['w-no-local', 'q-no-local'], ['w-no-local-claude', 'q-no-local-claude'], ['w-ready', 'q-ready'], ['w-checking', 'q-checking'], ['w-switch-off', 'q-switch-off'], ['w-own-omlx', 'q-own-omlx']];
for (const [file, frame] of WELCOME_FLOOR) both(file, frame, F);
// AI step states at the floor.
both('w-own-omlx-scrolled', 'q-own-omlx-scrolled', F);
both('w-terms-open', 'q-terms-open', F);
both('p-both-installing', 'p-both-installing', F);
both('p-cancelled', 'p-cancelled', F);
both('p-late-ready', 'p-late-ready', F);
both('w-slow-check-1280x900', 'q-slow-check', D);
both('p-key-and-claude', 'p-key-and-claude', F);
both('p-no-local-key', 'p-no-local-key', F);
both('p-local-fallback', 'p-local-fallback', F);
both('p-local-row-rest', 'p-local-row-rest', F);
both('p-starter-note', 'p-starter-note', F);
both('p-own-omlx-note', 'p-own-omlx-note', F);

const browser = await chromium.launch();
// Warm the dev server's module graph once, so no frame is the one that triggers its first reload.
{
	// It walks the real Welcome (and the own-oMLX one, with its popover) as the frames do, so every module a frame loads is served and
	// bundled before the first one runs: a first load mid-frame makes Vite reload the page under it.
	for (const setup of [undefined, FRAMES['q-own-omlx'].setup]) {
		const context = await browser.newContext({ viewport: { width: 800, height: 600 }, reducedMotion: 'reduce' });
		const world = fresh(setup);
		await mock(context, world);
		const warm = await context.newPage();
		try {
			await warm.goto(`${base}/`, { waitUntil: 'networkidle' });
			await warm.getByText('Which of these').first().waitFor({ timeout: 20000 });
			await warm.waitForTimeout(1500);
			await warm.getByRole('checkbox', { name: /^Claude/ }).check({ timeout: 5000 });
			await warm.getByRole('button', { name: 'What CORSIA changes', exact: true }).click({ timeout: 2000 }).catch(() => {});
			await warm.waitForTimeout(1500);
			// And on to the AI step (through the own oMLX consent where there is one), whose modules the first AI-step frame would
			// otherwise be the one to load.
			await warm.keyboard.press('Escape');
			await getStarted(warm);
			if (setup) await warm.getByRole('button', { name: 'Use it', exact: true }).click({ timeout: 5000 });
			await createAccount(warm);
			await warm.waitForTimeout(1500);
		} catch {
			// A failed warm-up only means the first frame may be the one that loads a module; the frames fail visibly if so.
		}
		await context.close();
	}
}
if (only) for (let i = frames.length - 1; i >= 0; i--) if (!only.includes(frames[i].frame)) frames.splice(i, 1);
const metrics = {};
const texts = {};
for (const spec of frames) {
	const def = FRAMES[spec.frame];
	const world = fresh(def.setup);
	const context = await browser.newContext({ viewport: { width: spec.w, height: spec.h }, deviceScaleFactor: 1, reducedMotion: 'reduce', colorScheme: spec.theme });
	await mock(context, world);
	// A frame shows a state at rest. Without this, a transition still under way (a switch thumb a fraction of a pixel short of its stop)
	// can be caught on a loaded machine, so two renders of one state differ by a few edge pixels.
	await context.addInitScript(() => {
		const style = document.createElement('style');
		style.textContent = '*, *::before, *::after { transition: none !important; animation-delay: 0s !important; }';
		document.addEventListener('DOMContentLoaded', () => document.head.append(style));
	});
	const page = await context.newPage();
	const failures = [];
	try {
		await def.drive(page, world);
		// The pointer a person left on the control they just used must not tint the frame: move it off before every shot.
		await page.mouse.move(0, 0);
		await page.evaluate(() => document.fonts.ready);
		await settle(page, 300);
	} catch (error) {
		failures.push(`could not reach the state through the UI: ${String(error).split('\n')[0]}`);
	}
	const text = (await page.evaluate(() => document.body.innerText)).replace(/ /g, ' ');
	for (const want of def.want) if (!text.includes(want)) failures.push(`missing "${want}"`);
	for (const no of def.no ?? []) if (text.includes(no)) failures.push(`should not show "${no}"`);
	for (const fix of def.fix ?? []) if (!text.includes(fix)) failures.push(`error frame without its fix: missing "${fix}"`);
	for (const failure of (await def.check?.(page, world)) ?? []) failures.push(failure);
	if (def.select) {
		const shown = await page.evaluate(() => { const select = document.querySelector('select[aria-label="Provider"]'); return select ? select.selectedOptions[0]?.textContent ?? '' : null; });
		if (shown !== def.select) failures.push(`the Provider select shows "${shown}", expected "${def.select}"`);
	}
	for (const name of def.disabled ?? []) {
		const disabled = await page.getByRole('button', { name }).first().isDisabled().catch(() => false);
		if (!disabled) failures.push(`"${name}" should be dimmed`);
	}
	if (def.focused && !(await page.evaluate((sel) => document.activeElement === document.querySelector(sel), def.focused))) failures.push(`${def.focused} should hold focus`);
	const m = await page.evaluate(() => {
		const body = document.querySelector('.body');
		const dock = document.querySelector('.pane > footer');
		const bottoms = body ? [...body.querySelectorAll('*')].map((e) => e.getBoundingClientRect()).filter((r) => r.height > 0).map((r) => r.bottom) : [];
		const switchEl = document.querySelector('button[aria-label="Also run a local model on this computer"]');
		return {
			switchY: switchEl ? switchEl.getBoundingClientRect().top : null,
			viewport: [innerWidth, innerHeight],
			lowest: bottoms.length ? Math.max(...bottoms) : 0,
			dockTop: dock ? dock.getBoundingClientRect().top : 0,
			scrollH: document.documentElement.scrollHeight,
			bodyOverflow: body ? body.scrollHeight > body.clientHeight + 1 : false,
		};
	});
	if (!m.dockTop) failures.push('no dock found: the setup screen is not what rendered');
	if (!def.floating && !(def.scrolls && m.viewport[1] <= 600) && m.lowest > m.dockTop - 8) failures.push(`content ends at ${Math.round(m.lowest)}, the dock starts at ${Math.round(m.dockTop)} (needs 8 px clear)`);
	// Welcome may scroll at the floor with the dock still in view (own oMLX adds two rows); nothing else may.
	if (def.scrolls && m.viewport[1] <= 600 && !m.bodyOverflow) failures.push('expected the body to scroll at the floor, with the dock docked');
	if (m.bodyOverflow && !def.scrolls) failures.push('the body scrolls');
	if (m.dockTop + 1 > m.viewport[1] || !m.dockTop) failures.push(`the dock is not in view (top ${Math.round(m.dockTop)} of ${m.viewport[1]})`);
	if (m.scrollH > m.viewport[1]) failures.push(`page scrolls (${m.scrollH} > ${m.viewport[1]})`);
	metrics[spec.file] = { ...m, frame: spec.frame, theme: spec.theme, size: `${spec.w}x${spec.h}`, failures };
	texts[spec.file] = text;
	await page.screenshot({ path: path.join(outDir, `${spec.file}.png`) });
	console.log(failures.length ? 'FAIL' : 'ok  ', spec.file, `room ${Math.round(m.dockTop - m.lowest)}px`, failures.join('; '));
	await context.close();
}
await browser.close();
// The question's height is reserved while CORSIA checks, so the switch block must not move when the check ends.
let moved = false;
for (const [slow, question] of [['w-slow-check-1280x900', 'c2-1-welcome-question-1280x900'], ['w-slow-check-1280x900-dark', 'c2-1-welcome-question-1280x900-dark']]) {
	if (!metrics[slow] || !metrics[question]) continue;
	const a = metrics[slow].switchY, b = metrics[question].switchY;
	console.log(`switch block y: ${slow} ${a?.toFixed(1)}  ${question} ${b?.toFixed(1)}  moved ${(a - b).toFixed(1)} px`);
	if (a == null || b == null || Math.abs(a - b) > 1) { moved = true; metrics[slow].failures.push(`the switch block sits at y ${a} while checking and ${b} once the question shows`); }
}
console.log('\nFloor table (800 x 600 Welcome states, light):');
for (const [file] of WELCOME_FLOOR) {
	const m = metrics[file];
	if (m) console.log(`${file.padEnd(20)} lowest element y ${String(Math.round(m.lowest)).padStart(4)}   dock top ${Math.round(m.dockTop)}   clear ${Math.round(m.dockTop - m.lowest)} px   ${m.failures.length ? 'FAIL' : 'ok'}`);
}
fs.writeFileSync(metricsFile, JSON.stringify(metrics, null, 1));
fs.writeFileSync(metricsFile.replace(/\.json$/, '') + '-text.json', JSON.stringify(texts, null, 1));
if (Object.values(metrics).some((m) => m.failures.length)) process.exit(1);
