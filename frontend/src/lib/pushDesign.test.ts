import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { IDLE_CAP_MS } from './ws/presence';
import { createHereSignal, type HereEnv, type HereFrame } from './ws/hereSignal';

/** R4's app-wide here signal is its own message (`here`), sent on change; this counts its frames with a fake clock. */
function run(minutes: number, inputEveryMs: number | null) {
	let clock = 1_000_000;
	let lastInput = inputEveryMs === null ? -Infinity : clock;
	const timers: { at: number; fn: () => void; live: boolean }[] = [];
	const sent: HereFrame[] = [];
	const env: HereEnv = {
		send: (f) => { sent.push(f); }, now: () => clock, lastInputAt: () => lastInput, isVisible: () => true,
		setTimer: (fn, ms) => { const t = { at: clock + ms, fn, live: true }; timers.push(t); return t; },
		clearTimer: (h) => { (h as { live: boolean }).live = false; },
	};
	const signal = createHereSignal(env);
	signal.update();
	const end = clock + minutes * 60_000;
	let nextInput = inputEveryMs === null ? Infinity : clock + inputEveryMs;
	while (clock < end) {
		const due = timers.filter((t) => t.live).sort((a, b) => a.at - b.at)[0];
		const next = Math.min(due ? due.at : Infinity, nextInput, end);
		clock = next;
		if (next === nextInput) { lastInput = clock; signal.update(); nextInput += inputEveryMs!; continue; }
		if (due && due.at === next) { due.live = false; due.fn(); }
	}
	return sent.map((f) => f.state);
}

describe('app-wide here signal message rate (R4)', () => {
	it('an open page with no input for an hour sends nothing', () => {
		const frames = run(60, null);
		expect(frames.length).toBe(0); // no input ever: never here, so never sends
	});
	it('input once, then a quiet hour: one claim and one idle frame', () => {
		const frames = run(60, 11 * 60_000 + 1); // one input at start, the next after the 10-minute cap
		expect(frames.filter((s) => s === 'away').length).toBeGreaterThanOrEqual(1);
		expect(frames.length / 60).toBeLessThan(0.5); // frames per minute
	});
	it('hidden says so, away after idle is plain, and a reconnect says here again; no frame names a thread', () => {
		let clock = 5_000_000; let visible = true; const sent: HereFrame[] = [];
		const timers: (() => void)[] = [];
		const signal = createHereSignal({ send: (f) => sent.push(f), now: () => clock, lastInputAt: () => clock, isVisible: () => visible, setTimer: (fn) => { timers.push(fn); return timers.length; }, clearTimer: () => undefined });
		signal.update();
		signal.reconnected();
		visible = false; signal.update();
		expect(sent).toEqual([{ type: 'here', state: 'here' }, { type: 'here', state: 'here' }, { type: 'here', state: 'away', hidden: true }]);
		expect(JSON.stringify(sent)).not.toMatch(/thread/);
	});
	it('typing every 30 seconds for an hour: one claim frame, no more', () => {
		const frames = run(60, 30_000);
		expect(frames).toEqual(['here']);
	});
	it('the cap is today\'s', () => { expect(IDLE_CAP_MS).toBe(600_000); });
});

/** The worker file, run against a fake `self`. */
function load() {
	const handlers: Record<string, (e: unknown) => void> = {};
	const shown: { title: string; options: Record<string, unknown> }[] = [];
	const opened: string[] = [];
	const messages: unknown[] = [];
	let clients: { url: string }[] = [];
	const self = {
		location: { origin: 'https://corsia.example' },
		addEventListener: (type: string, fn: (e: unknown) => void) => { handlers[type] = fn; },
		registration: { showNotification: async (title: string, options: Record<string, unknown>) => { shown.push({ title, options }); } },
		clients: {
			matchAll: async () => clients.map((c) => ({ ...c, focus: async () => undefined, postMessage: (m: unknown) => { messages.push(m); } })),
			openWindow: async (u: string) => { opened.push(u); },
		},
	};
	new Function('self', 'URL', readFileSync('static/push-sw.js', 'utf8'))(self, URL);
	const wait = async (event: { _p?: Promise<unknown> }) => { await event._p; };
	const event = (extra: object) => { const e: { _p?: Promise<unknown>; waitUntil: (p: Promise<unknown>) => void } = { waitUntil: (p: Promise<unknown>) => { e._p = p; }, ...extra } as never; return e; };
	return { handlers, shown, opened, messages, wait, event, setClients: (c: { url: string }[]) => { clients = c; } };
}

describe('push-sw.js', () => {
	it('registers only push and notificationclick: no fetch, install, activate', () => {
		const w = load();
		expect(Object.keys(w.handlers).sort()).toEqual(['notificationclick', 'push']);
		const source = readFileSync('static/push-sw.js', 'utf8').replace(/\/\/.*$/gm, '');
		expect(source).not.toMatch(/clients\.claim|skipWaiting|unregister\(/);
	});
	it('always shows a notification: a readable payload, then garbage, then none', async () => {
		const w = load();
		for (const data of [{ json: () => ({ title: 'T', body: 'B', tag: '/?activity=a', path: '/?activity=a' }) }, { json: () => { throw new Error('x'); } }, null]) {
			const e = w.event({ data });
			w.handlers.push(e); await w.wait(e);
		}
		expect(w.shown.map((s) => s.title)).toEqual(['T', 'Something needs you', 'Something needs you']);
		expect(w.shown[1].options.body).toBe('Open CORSIA to see it.');
	});
	it('a tap opens an Activity item or the home page, never another place', async () => {
		const w = load();
		for (const path of ['/?activity=item-1', '//evil.example', '/\\evil.example', 'https://evil.example', '/x']) {
			const e = w.event({ notification: { close() {}, data: { path } } });
			w.handlers.notificationclick(e); await w.wait(e);
		}
		expect(w.opened).toEqual([
			'https://corsia.example/?activity=item-1', 'https://corsia.example/', 'https://corsia.example/', 'https://corsia.example/', 'https://corsia.example/',
		]);
	});
	it('a tap focuses an open window of this origin and hands it the path; it never navigates', async () => {
		const w = load();
		w.setClients([{ url: 'https://elsewhere.example/' }, { url: 'https://corsia.example/' }]);
		const e = w.event({ notification: { close() {}, data: { path: '/?activity=item-1' } } });
		w.handlers.notificationclick(e); await w.wait(e);
		expect(w.messages).toEqual([{ type: 'corsia-open-activity', path: '/?activity=item-1' }]);
		expect(w.opened).toEqual([]);
		expect(readFileSync('static/push-sw.js', 'utf8').replace(/\/\/.*$/gm, '')).not.toMatch(/navigate\(/);
	});
	it('a tap with a bad path hands the window only the home page', async () => {
		const w = load();
		w.setClients([{ url: 'https://corsia.example/' }]);
		const e = w.event({ notification: { close() {}, data: { path: '//evil.example' } } });
		w.handlers.notificationclick(e); await w.wait(e);
		expect(w.messages).toEqual([{ type: 'corsia-open-activity', path: '/' }]);
	});
});

/** R17: app.html's worker clean-up, run as it ships against fake registrations. */
describe('app.html worker clean-up (R17)', () => {
	const html = readFileSync('src/app.html', 'utf8');
	const block = html.match(/<script>\s*(if \('serviceWorker' in navigator\) \{[\s\S]*?\n\t\t\})\s*\n\t<\/script>/);
	const reg = (url: string, slot: 'active' | 'waiting' | 'installing' = 'active') => {
		const r = { [slot]: { scriptURL: url }, unregistered: false, unregister() { r.unregistered = true; return Promise.resolve(true); } } as Record<string, unknown>;
		return r as { unregistered: boolean };
	};
	const load = async (regs: { unregistered: boolean }[]) => {
		new Function('navigator', block![1])({ serviceWorker: { getRegistrations: () => Promise.resolve(regs) } });
		await new Promise((r) => setTimeout(r, 0));
	};
	it('finds the clean-up script', () => { expect(block).not.toBeNull(); });
	it('the push worker stays on the second page load, whichever slot it is in; a stale sw.js is removed', async () => {
		const push = [reg('https://x/push-sw.js'), reg('https://x/push-sw.js', 'waiting'), reg('https://x/push-sw.js', 'installing')];
		const stale = reg('https://x/sw.js');
		const other = reg('https://x/other.js');
		await load([...push, stale, other]);
		expect(push.map((r) => r.unregistered)).toEqual([false, false, false]);
		expect(stale.unregistered).toBe(true);
		expect(other.unregistered).toBe(true);
	});
	it('a lookalike name is not the push worker', async () => {
		const fake = reg('https://x/evil-push-sw.js.map');
		await load([fake]);
		expect(fake.unregistered).toBe(true);
	});
});
