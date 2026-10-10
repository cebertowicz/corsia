#!/usr/bin/env node
// Measure opening a seeded thread in the browser, Focus on and off.
//
//   node measure.mjs --base http://127.0.0.1:3231 --token-file T --threads threads.json \
//        --runs 3 --out results.json [--only label,label]
//
// Uses the playwright package from ../../frontend/node_modules and a fresh browser
// context (empty storage, cold HTTP cache) per run. Focus is switched through the
// real PATCH /api/settings route. The app is loaded first, then the thread is
// clicked in the sidebar; t0 is the click.
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.join(here, '../../frontend/package.json'));
const { chromium } = require('@playwright/test');

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => {
	if (a.startsWith('--')) acc.push([a.slice(2), all[i + 1]]);
	return acc;
}, []));
const base = args.base;
const token = fs.readFileSync(args['token-file'], 'utf8').trim();
const threads = JSON.parse(fs.readFileSync(args.threads, 'utf8'));
const runs = Number(args.runs ?? 3);
const only = args.only ? new Set(args.only.split(',')) : null;

async function setFocus(on) {
	const res = await fetch(`${base}/api/settings`, {
		method: 'PATCH',
		headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
		body: JSON.stringify({ features: { focus_mode: on } }),
	});
	if (!res.ok) throw new Error(`settings PATCH ${res.status}`);
}

const median = (xs) => {
	const s = [...xs].sort((a, b) => a - b);
	return s.length ? s[Math.floor((s.length - 1) / 2)] : null;
};

async function settle(state, quietMs = 1200, timeoutMs = 90000) {
	const start = Date.now();
	while (Date.now() - start < timeoutMs) {
		await new Promise((r) => setTimeout(r, 100));
		if (state.inflight === 0 && Date.now() - state.lastActivity > quietMs) return true;
	}
	return false;
}

async function runOnce(browser, label, threadId, focus) {
	const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
	const page = await context.newPage();
	const state = { inflight: 0, lastActivity: Date.now(), reqs: [], phase: 'load' };
	await page.addInitScript(() => {
		window.__first = null;
		new MutationObserver(() => {
			if (window.__first === null && window.__prefix && document.querySelector(`.message[data-message-id^="${window.__prefix}-"]`)) {
				window.__first = performance.now();
			}
		}).observe(document, { childList: true, subtree: true });
	});
	page.on('request', (req) => {
		if (!req.url().includes('/api/')) return;
		state.inflight += 1;
		state.lastActivity = Date.now();
		req.__t = Date.now();
	});
	const done = async (req, res) => {
		if (!req.url().includes('/api/')) return;
		state.inflight -= 1;
		state.lastActivity = Date.now();
		const url = new URL(req.url());
		const entry = {
			phase: state.phase, path: url.pathname, query: url.search.replace(/token=[^&]*/, 'token=_'),
			endAt: Date.now(), ms: Date.now() - req.__t, status: res ? res.status() : 0,
		};
		const timing = req.timing();
		entry.ttfbMs = timing.responseStart > 0 ? Math.round(timing.responseStart - timing.requestStart) : null;
		if (res && url.pathname.endsWith(`/threads/${threadId}/messages`)) {
			try {
				const body = await res.json();
				entry.messages = body.messages.length;
				entry.hasMore = body.has_more;
				entry.bytes = JSON.stringify(body).length;
			} catch { /* body gone */ }
		}
		state.reqs.push(entry);
	};
	page.on('requestfinished', async (req) => { await done(req, await req.response()); });
	page.on('requestfailed', async (req) => { await done(req, null); });

	await page.goto(`${base}/?token=${token}`);
	await page.getByRole('button', { name: 'Toggle sidebar' }).waitFor({ timeout: 30000 });
	const title = `focus-baseline ${label}`;
	const row = page.getByText(title, { exact: true }).first();
	await row.waitFor({ timeout: 30000 });
	await settle(state, 800, 30000);
	state.phase = 'open';
	state.reqs.length = 0;
	const t0 = await page.evaluate((id) => { window.__first = null; window.__prefix = id; return performance.now(); }, threadId);
	const tClick = Date.now();
	await row.click();
	const firstVisible = await page.waitForFunction(() => window.__first !== null && window.__first, null, { timeout: 90000 })
		.then((h) => h.jsonValue()).catch(() => null);
	const firstMs = firstVisible === null ? null : Math.round(firstVisible - t0);
	const settled = await settle(state);
	const openReqs = state.reqs.slice();
	const domMessages = await page.locator(`.message[data-message-id^="${threadId}-"]`).count();

	// Scroll up once.
	state.phase = 'scroll';
	await page.evaluate(() => { const el = document.querySelector('.messages'); if (el) el.scrollTop = 0; });
	await settle(state);
	const scrollReqs = state.reqs.filter((r) => r.phase === 'scroll');
	await context.close();

	const pages = (rs) => rs.filter((r) => r.path.endsWith(`/threads/${threadId}/messages`));
	const sum = (rs, k) => rs.reduce((a, r) => a + (r[k] ?? 0), 0);
	return {
		firstVisibleMs: firstMs,
		settled,
		openTotalMs: Math.max(0, ...openReqs.map((r) => r.endAt)) - tClick,
		openPages: pages(openReqs).length,
		openMessagesFetched: sum(pages(openReqs), 'messages'),
		openBytes: sum(pages(openReqs), 'bytes'),
		pageMs: pages(openReqs).map((r) => r.ms),
		pageTtfbMs: pages(openReqs).map((r) => r.ttfbMs),
		domMessages,
		scrollPages: pages(scrollReqs).length,
		scrollMessagesFetched: sum(pages(scrollReqs), 'messages'),
		sidebar: openReqs.filter((r) => !r.path.endsWith('/messages')).map((r) => ({ path: r.path, ms: r.ms, ttfbMs: r.ttfbMs })),
	};
}

const browser = await chromium.launch();
const results = {};
for (const [label, threadId] of Object.entries(threads)) {
	if (only && !only.has(label)) continue;
	for (const focus of [true, false]) {
		await setFocus(focus);
		const key = `${label}|focus=${focus}`;
		results[key] = { threadId, runs: [] };
		for (let i = 0; i < runs; i++) {
			const r = await runOnce(browser, label, threadId, focus);
			results[key].runs.push(r);
			console.error(key, JSON.stringify({ first: r.firstVisibleMs, total: r.openTotalMs, pages: r.openPages, msgs: r.openMessagesFetched, scrollPages: r.scrollPages }));
		}
		const rs = results[key].runs;
		results[key].median = {
			firstVisibleMs: median(rs.map((r) => r.firstVisibleMs).filter((x) => x !== null)),
			openTotalMs: median(rs.map((r) => r.openTotalMs)),
			openPages: median(rs.map((r) => r.openPages)),
			openMessagesFetched: median(rs.map((r) => r.openMessagesFetched)),
			scrollPages: median(rs.map((r) => r.scrollPages)),
			scrollMessagesFetched: median(rs.map((r) => r.scrollMessagesFetched)),
		};
	}
}
await browser.close();
fs.writeFileSync(args.out, JSON.stringify(results, null, 1));
