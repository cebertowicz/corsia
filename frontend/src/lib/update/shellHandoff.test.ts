import { afterEach, describe, expect, it } from 'vitest';
import { encodeHandoff, HANDOFF_MAX_CHARS, parseHandoff, readHandoff, returnUrl, safeReturnRoute, routeToRestore, takeHandoffFromLocation, type ScreenHandoff } from './shellHandoff';

const NOW = 1_800_000_000_000;

function screen(overrides: Partial<ScreenHandoff> = {}): ScreenHandoff {
	return {
		v: 1, operationId: 'update-1',
		model: { value: 41.3, step: 'upgrading', mode: 'migration', lifted: 0, visitedMigration: true, lastAt: NOW - 1_000 },
		fromVersion: '2026.10.12', toVersion: '2026.10.13', usualSeconds: 45,
		downAt: NOW - 70_000, seqChangedAt: NOW - 61_000, lastSeq: 12, lastPid: 4242,
		failure: null, theme: 'light', route: '/?project=abc',
		...overrides,
	};
}

afterEach(() => history.replaceState(null, '', '/'));

describe('the update screen handoff', () => {
	it('round-trips the whole screen through a fragment well inside the bound', () => {
		const fragment = `#${encodeHandoff(screen())}`;
		expect(fragment.length).toBeLessThan(HANDOFF_MAX_CHARS / 2);
		expect(readHandoff(fragment, NOW)).toEqual(screen());
	});

	it.each([
		['another version', { v: 2 }],
		['a percentage past 100', { model: { ...screen().model, value: 140 } }],
		['an unknown step', { model: { ...screen().model, step: 'start_fresh' } }],
		['a clock from another day', { downAt: NOW - 3 * 86_400_000 }],
		['a fractional pid', { lastPid: 1.5 }],
		['an unknown failure', { failure: { code: 'adopt', verdict: false } }],
		['markup for a version', { toVersion: '<img src=x onerror=alert(1)>' }],
		['an unknown theme', { theme: 'sepia' }],
		['an action', { action: 'manual' }],
	])('refuses %s', (_name, change) => {
		const forged = { ...screen(), ...change } as Record<string, unknown>;
		const parsed = parseHandoff(forged, NOW);
		if ('action' in change) expect(parsed && 'action' in parsed).toBe(false);
		else expect(parsed).toBeNull();
	});

	it('ignores fragments too long, not its own, or not JSON', () => {
		expect(readHandoff(`#corsia-update=${'x'.repeat(HANDOFF_MAX_CHARS)}`, NOW)).toBeNull();
		expect(readHandoff('#background-approval', NOW)).toBeNull();
		expect(readHandoff('#corsia-update=%7Bnope', NOW)).toBeNull();
	});

	it.each(['//evil.example/', 'https://evil.example/', 'javascript:alert(1)', '/a\\b', 'relative', '/a b', `/${'a'.repeat(600)}`])('never returns to %s', (route) => {
		expect(safeReturnRoute(route)).toBeNull();
		expect(parseHandoff({ ...screen(), route }, NOW)).toBeNull();
	});

	it('keeps a same-origin path and query, without a fragment', () => {
		expect(safeReturnRoute('/settings?section=system#x')).toBe('/settings?section=system');
	});

	it('returns only to the loopback service, carrying the screen', () => {
		const target = returnUrl('http://127.0.0.1:47895', screen());
		expect(target?.startsWith('http://127.0.0.1:47895/?project=abc#corsia-update=')).toBe(true);
		expect(readHandoff(new URL(target!).hash, NOW)).toEqual(screen());
		for (const url of ['https://127.0.0.1:47895', 'http://evil.example', 'tauri://localhost', 'not a url']) expect(returnUrl(url, screen())).toBeNull();
	});

	it('takes the handoff out of the address so a reload never replays it', () => {
		history.replaceState(null, '', `/chat?x=1#${encodeHandoff(screen({ downAt: Date.now(), seqChangedAt: Date.now(), model: { ...screen().model, lastAt: Date.now() } }))}`);
		expect(takeHandoffFromLocation()?.operationId).toBe('update-1');
		expect(window.location.hash).toBe('');
		expect(`${window.location.pathname}${window.location.search}`).toBe('/chat?x=1');
		expect(takeHandoffFromLocation()).toBeNull();
	});
});

describe('the page a handed-back screen restores', () => {
	it('is the kept route when the window arrived somewhere else, and nothing when it is already there', () => {
		expect(routeToRestore('/?project=abc', '/', '')).toBe('/?project=abc');
		expect(routeToRestore('/?project=abc', '/', '?project=abc')).toBeNull();
		expect(routeToRestore('/', '/', '')).toBeNull();
		expect(routeToRestore('/settings', '/', '?project=abc')).toBe('/settings');
	});
});
