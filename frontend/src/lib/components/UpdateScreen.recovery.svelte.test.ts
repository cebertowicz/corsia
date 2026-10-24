import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import UpdateScreen from './UpdateScreen.svelte';
import { updateScreenView } from '$lib/update/screenStore';
import { readHandoff, type ScreenHandoff } from '$lib/update/shellHandoff';
import { launchReadyUrl, startupProgress } from '$lib/update/startupProgress';

const state = vi.hoisted(() => ({ shellPage: false, moves: [] as string[], route: '/' }));
const NOW = Date.UTC(2026, 9, 24, 21, 0);

const screen: ScreenHandoff = {
	v: 1, operationId: 'update-1',
	model: { value: 52.4, step: 'upgrading', mode: 'migration', lifted: 0, visitedMigration: true, lastAt: NOW },
	fromVersion: '2026.10.12', toVersion: '2026.10.13', usualSeconds: 45,
	downAt: NOW - 90_000, seqChangedAt: NOW - 61_000, lastSeq: 7, lastPid: 4242, failure: null, theme: 'light', route: '/',
};

vi.mock('$lib/remoteShell', async (original) => ({ ...(await original<typeof import('$lib/remoteShell')>()), onStartupShellPage: () => state.shellPage }));
vi.mock('$lib/update/screenStore', async (original) => ({ ...(await original<typeof import('$lib/update/screenStore')>()), handOverScreen: (_theme: string, route: string) => ({ ...screen, route }) }));
vi.mock('$lib/update/shellHandoff', async (original) => ({
	...(await original<typeof import('$lib/update/shellHandoff')>()),
	moveWindow: (url: string) => { state.moves.push(url); },
	keptReturnRoute: () => state.route,
}));

type View = NonNullable<Parameters<typeof updateScreenView.set>[0]>;
const longer: View = { mode: 'longer', step: 'upgrading', label: 'Upgrading your data', percent: 52.4, estimated: false, fromVersion: '2026.10.12', toVersion: '2026.10.13', failure: null, serviceDown: true };
const startFailed: View = { ...longer, mode: 'start_failed' };

let component: Record<string, unknown> | null = null;
let invoke: ReturnType<typeof vi.fn>;
let answers: unknown[];

function show(view: View): void {
	updateScreenView.set(view);
	component = mount(UpdateScreen, { target: document.body }) as Record<string, unknown>;
	flushSync();
}

async function settle(): Promise<void> {
	for (let i = 0; i < 5; i += 1) await tick();
	flushSync();
}

const text = () => document.body.textContent ?? '';
const startButton = () => [...document.querySelectorAll('button')].find((button) => button.textContent?.includes('Start CORSIA'));

beforeEach(() => {
	vi.setSystemTime(NOW);
	state.shellPage = false;
	state.moves = [];
	state.route = '/';
	answers = [];
	invoke = vi.fn(async () => answers.shift() ?? { recovery: 'unavailable' });
	const shell = window as unknown as Record<string, unknown>;
	shell.__CORSIA_SHELL__ = { mode: 'local' };
	shell.__TAURI_INTERNALS__ = { invoke };
});

afterEach(() => {
	component && unmount(component);
	component = null;
	updateScreenView.set(null);
	startupProgress.set(null);
	launchReadyUrl.set(null);
	const shell = window as unknown as Record<string, unknown>;
	delete shell.__CORSIA_SHELL__;
	delete shell.__TAURI_INTERNALS__;
	vi.useRealTimers();
});

describe('the update screen on the page the service serves', () => {
	it.each([['ran long', longer], ['did not start', startFailed]])('hands the screen to the shell page when it %s, and never asks for a start itself', async (_name, view) => {
		show(view);
		await settle();
		expect(invoke).not.toHaveBeenCalled();
		expect(state.moves).toHaveLength(1);
		const [page, fragment] = state.moves[0].split('#');
		expect(page).toBe('tauri://localhost/update.html');
		expect(readHandoff(fragment, NOW)).toMatchObject({ operationId: 'update-1', model: { value: 52.4 }, route: '/' });
	});

	it('hands over once, and again only when the person presses Start CORSIA', async () => {
		show(startFailed);
		await settle();
		updateScreenView.set({ ...startFailed, percent: 52.5 });
		await settle();
		expect(state.moves).toHaveLength(1);
		startButton()?.click();
		await settle();
		expect(state.moves).toHaveLength(2);
		expect(invoke).not.toHaveBeenCalled();
	});

	it('keeps its latest screen in its own address while it shows, for a reopened app to carry, and takes it out after', async () => {
		history.replaceState(null, '', '/?project=abc');
		show({ ...longer, mode: 'progress' });
		await settle();
		expect(`${window.location.pathname}${window.location.search}`).toBe('/?project=abc');
		expect(readHandoff(window.location.hash, NOW)).toMatchObject({ operationId: 'update-1', model: { value: 52.4 }, route: '/?project=abc' });
		updateScreenView.set(null);
		await settle();
		expect(window.location.hash).toBe('');
		expect(`${window.location.pathname}${window.location.search}`).toBe('/?project=abc');
		expect(invoke).not.toHaveBeenCalled();
		history.replaceState(null, '', '/');
	});

	it('keeps nothing in the address of a remote window', async () => {
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote', name: 'Studio' };
		show({ ...longer, mode: 'progress' });
		await settle();
		expect(window.location.hash).toBe('');
	});

	it('stays put for a remote shell and for the plain progress state', async () => {
		show({ ...longer, mode: 'progress' });
		await settle();
		expect(state.moves).toEqual([]);
		component && unmount(component);
		(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote', name: 'Studio' };
		show(longer);
		await settle();
		expect(state.moves).toEqual([]);
		expect(invoke).not.toHaveBeenCalled();
	});
});

describe('the update screen on the shell page', () => {
	beforeEach(() => { state.shellPage = true; });

	it('asks once for an automatic start when the wait runs long, and returns to the kept page on Ready', async () => {
		answers = [{ recovery: 'ready', url: 'http://127.0.0.1:47895' }];
		state.route = '/?project=abc';
		show(longer);
		await settle();
		expect(invoke.mock.calls).toEqual([['start_local_service', { recovery: 'automatic' }]]);
		expect(state.moves).toHaveLength(1);
		const target = new URL(state.moves[0]);
		expect(`${target.origin}${target.pathname}${target.search}`).toBe('http://127.0.0.1:47895/?project=abc');
		expect(readHandoff(target.hash, NOW)).toMatchObject({ operationId: 'update-1', model: { value: 52.4 } });
		// Ready returns to the service's page, whose verifier confirms the version; this screen never claims done.
		expect(text()).not.toContain('Updated to');
	});

	it('waits on a deferred start, asking again on the screen’s next second only', async () => {
		answers = [{ recovery: 'deferred' }, { recovery: 'deferred' }];
		show(longer);
		await settle();
		expect(invoke).toHaveBeenCalledTimes(1);
		expect(text()).toContain('CORSIA is still starting. Nothing you need to do yet.');
		expect(startButton()).toBeUndefined();
		// Recomputes inside the same second (a progress read and the ticker both recompute) ask nothing more.
		vi.setSystemTime(NOW + 999);
		updateScreenView.set({ ...longer, percent: 52.4 });
		await settle();
		expect(invoke).toHaveBeenCalledTimes(1);
		vi.setSystemTime(NOW + 1_000);
		updateScreenView.set({ ...longer, percent: 52.4 });
		await settle();
		expect(invoke).toHaveBeenCalledTimes(2);
		expect(invoke.mock.calls.every(([, args]) => (args as { recovery: string }).recovery === 'automatic')).toBe(true);
		expect(text()).toContain('CORSIA is still starting. Nothing you need to do yet.');
	});

	it.each(['unavailable', 'not_started'])('offers Start CORSIA after an automatic start that is %s, and asks for a manual one only on its press', async (recovery) => {
		answers = [{ recovery }, { recovery }];
		show(longer);
		await settle();
		expect(text()).toContain('It didn’t start on its own.');
		updateScreenView.set({ ...longer, percent: 52.4 });
		await settle();
		expect(invoke.mock.calls).toEqual([['start_local_service', { recovery: 'automatic' }]]);
		startButton()?.click();
		await settle();
		expect(invoke.mock.calls).toEqual([['start_local_service', { recovery: 'automatic' }], ['start_local_service', { recovery: 'manual' }]]);
		expect(text()).toContain('It still didn’t start.');
		expect(state.moves).toEqual([]);
	});

	it('never starts anything on its own for a start failure until the person presses Start CORSIA', async () => {
		answers = [{ recovery: 'ready', url: 'http://127.0.0.1:47895' }];
		show(startFailed);
		await settle();
		expect(invoke).not.toHaveBeenCalled();
		expect(state.moves).toEqual([]);
		startButton()?.click();
		await settle();
		expect(invoke.mock.calls).toEqual([['start_local_service', { recovery: 'manual' }]]);
		expect(state.moves).toHaveLength(1);
	});

	it('does not leave for a Ready answer that is not this computer’s service', async () => {
		answers = [{ recovery: 'ready', url: 'https://evil.example' }];
		show(longer);
		await settle();
		expect(state.moves).toEqual([]);
		expect(text()).toContain('It didn’t start on its own.');
	});

	const intents = () => invoke.mock.calls.map(([, args]) => (args as { recovery: string }).recovery);
	const recomputeAt = async (at: number) => {
		vi.setSystemTime(at);
		updateScreenView.set({ ...longer, percent: 52.4 });
		await settle();
	};

	it('keeps a deferred press a press: asked again as itself once a second, never as the spent automatic start, until it is answered', async () => {
		answers = [{ recovery: 'unavailable' }, { recovery: 'deferred' }, { recovery: 'deferred' }, { recovery: 'ready', url: 'http://127.0.0.1:47895' }];
		state.route = '/?project=abc';
		show(longer);
		await settle();
		startButton()?.click();
		await settle();
		expect(intents()).toEqual(['automatic', 'manual']);
		expect(text()).toContain('CORSIA is still starting. Nothing you need to do yet.');
		expect(startButton()).toBeUndefined();
		// Inside the second: nothing more is asked.
		await recomputeAt(NOW + 999);
		expect(intents()).toEqual(['automatic', 'manual']);
		// The next second: the same press, deferred again, and still only waiting.
		await recomputeAt(NOW + 1_000);
		expect(intents()).toEqual(['automatic', 'manual', 'manual']);
		expect(text()).toContain('CORSIA is still starting. Nothing you need to do yet.');
		expect(text()).not.toContain('It still didn’t start.');
		expect(state.moves).toEqual([]);
		// The second after: the lock is free, the press runs once, and Ready returns to the kept page.
		await recomputeAt(NOW + 2_000);
		expect(intents()).toEqual(['automatic', 'manual', 'manual', 'manual']);
		expect(state.moves).toHaveLength(1);
		expect(new URL(state.moves[0]).search).toBe('?project=abc');
		// Answered: nothing is asked again.
		await recomputeAt(NOW + 3_000);
		expect(invoke).toHaveBeenCalledTimes(4);
	});

	it('says it still didn’t start only after the deferred press really ran and failed', async () => {
		answers = [{ recovery: 'unavailable' }, { recovery: 'deferred' }, { recovery: 'not_started' }];
		show(longer);
		await settle();
		startButton()?.click();
		await settle();
		expect(text()).not.toContain('It still didn’t start.');
		await recomputeAt(NOW + 1_000);
		expect(intents()).toEqual(['automatic', 'manual', 'manual']);
		expect(text()).toContain('It still didn’t start.');
		expect(startButton()).toBeDefined();
		await recomputeAt(NOW + 2_000);
		expect(invoke).toHaveBeenCalledTimes(3);
	});

	it('never replays a deferred press after a reload', async () => {
		answers = [{ recovery: 'unavailable' }, { recovery: 'deferred' }];
		show(longer);
		await settle();
		startButton()?.click();
		await settle();
		component && unmount(component);
		component = null;
		invoke.mockClear();
		vi.setSystemTime(NOW + 5_000);
		show(longer);
		await settle();
		await recomputeAt(NOW + 7_000);
		expect(intents()).not.toContain('manual');
	});

	it('keeps waiting for a deferred press on a start failure too', async () => {
		answers = [{ recovery: 'deferred' }];
		show(startFailed);
		await settle();
		startButton()?.click();
		await settle();
		updateScreenView.set({ ...startFailed, percent: 52.4 });
		await settle();
		expect(text()).toContain('CORSIA is still starting. Nothing you need to do yet.');
		expect(text()).not.toContain('It didn’t start on its own.');
		expect(startButton()).toBeUndefined();
	});

	it('returns to the kept page once the shell reads that the service is back, with no start of its own', async () => {
		answers = [{ recovery: 'unavailable' }];
		state.route = '/?project=abc';
		show(longer);
		await settle();
		expect(text()).toContain('It didn’t start on its own.');
		startupProgress.set({ file: null, readAt: NOW, readError: null, readyUrl: 'http://127.0.0.1:47895' });
		await settle();
		expect(invoke).toHaveBeenCalledTimes(1);
		expect(state.moves).toHaveLength(1);
		const target = new URL(state.moves[0]);
		expect(`${target.origin}${target.pathname}${target.search}`).toBe('http://127.0.0.1:47895/?project=abc');
		expect(readHandoff(target.hash, NOW)).toMatchObject({ operationId: 'update-1' });
	});

	it('ignores a ready address that is not this computer’s service', async () => {
		show(longer);
		await settle();
		startupProgress.set({ file: null, readAt: NOW, readError: null, readyUrl: 'https://evil.example' });
		await settle();
		expect(state.moves).toEqual([]);
	});

	it('follows the reopened app’s own Ready launch answer to the kept page, asking nothing', async () => {
		state.route = '/?project=abc';
		show({ ...longer, mode: 'progress' });
		await settle();
		launchReadyUrl.set('http://127.0.0.1:47895/#corsia-update=older');
		await settle();
		expect(invoke).not.toHaveBeenCalled();
		expect(state.moves).toHaveLength(1);
		const target = new URL(state.moves[0]);
		expect(`${target.origin}${target.pathname}${target.search}`).toBe('http://127.0.0.1:47895/?project=abc');
		expect(readHandoff(target.hash, NOW)).toMatchObject({ operationId: 'update-1' });
	});
});

describe('a ready address on the service’s own page', () => {
	it('is never followed there: that page already is the service', async () => {
		show(longer);
		await settle();
		const moves = state.moves.length;
		startupProgress.set({ file: null, readAt: NOW, readError: null, readyUrl: 'http://127.0.0.1:47895' });
		await settle();
		expect(state.moves).toHaveLength(moves);
		expect(state.moves.every((move) => move.startsWith('tauri://localhost/update.html#'))).toBe(true);
	});
});
