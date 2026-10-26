import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ConnectedAppsSettings from './ConnectedAppsSettings.svelte';
import { token } from '$lib/stores/auth';
import { projects } from '$lib/stores/projects';
import type { ConnectedApp } from '$lib/api/connectedApps';

const reach = { mode: 'all' as const, project_ids: [], outside_projects: true };
const figma = (patch: Partial<ConnectedApp> = {}): ConnectedApp => ({
	id: 'figma', name: 'Figma', state: 'needs_sign_in', revision: 1, reach, sign_in: { status: 'idle', signed_in: false }, ...patch,
});
const failedSignIn = (failureClass: string) => figma({ sign_in: { status: 'failed', signed_in: false, failure_class: failureClass } });
const reply = (body: unknown, ok = true) => ({ ok, status: ok ? 200 : 502, json: async () => body, clone() { return this; } });
let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ measureText: (text: string) => ({ width: text.length * 8 }) } as CanvasRenderingContext2D);
	token.set('test-token');
	projects.set([]);
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	token.set(null);
});

const button = (label: string): HTMLButtonElement | undefined => [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);
const rowButtons = (): string[] => [...host.querySelectorAll('.item .btn')].map((b) => b.textContent?.trim() ?? '');

/** The list answers with `rows`; POST /reconnect and DELETE answer through the given handlers. */
function stubApi(rows: () => ConnectedApp[], handlers: { reconnect?: () => Promise<unknown>; remove?: () => Promise<unknown> } = {}) {
	const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		if (url.includes('/reconnect')) return handlers.reconnect ? handlers.reconnect() : reply({});
		if (init?.method === 'DELETE') return handlers.remove ? handlers.remove() : reply({});
		return reply({ servers: rows(), sign_in_here: true });
	});
	vi.stubGlobal('fetch', fetch);
	return fetch;
}

async function openRow(name: string): Promise<void> {
	component = mount(ConnectedAppsSettings, { target: host });
	await vi.waitFor(() => expect(host.querySelector(`[data-settings-row="${name}"]`)).not.toBeNull());
	const toggle = host.querySelector<HTMLButtonElement>(`[data-settings-row="${name}"] button.open`);
	if (toggle?.getAttribute('aria-expanded') !== 'true') { toggle?.click(); flushSync(); }
}

describe('a hosted app row: state to actions', () => {
	it.each([
		['working', figma({ state: 'working', sign_in: { status: 'idle', signed_in: false } }), ['Where it works…', 'Reconnect', 'Remove']],
		['failed to start', figma({ state: 'failed', failure_class: 'cannot_start', sign_in: undefined }), ['Where it works…', 'Reconnect', 'Remove']],
		['needs sign-in', figma(), ['Sign in', 'Where it works…', 'Remove']],
		['sign-in failed, retryable', failedSignIn('app_busy'), ['Try again', 'Where it works…', 'Remove']],
		['sign-in failed, no client ID', failedSignIn('address_mismatch'), ['Sign in', 'Where it works…', 'Remove']],
		['sign-in refused by the app', failedSignIn('not_allowed'), ['Where it works…', 'Remove']],
		['sign-in cannot get the access it needs', failedSignIn('permissions'), ['Where it works…', 'Remove']],
	])('%s', async (_name, app, buttons) => {
		stubApi(() => [app]);
		await openRow('Figma');
		expect(rowButtons()).toEqual(buttons);
	});
});

describe('a hosted app row: pressing Reconnect', () => {
	it('keeps the row when the answer is only the new state', async () => {
		let answered = false;
		stubApi(
			() => { if (answered) throw new Error('offline'); return [figma({ state: 'failed', failure_class: 'cannot_start', sign_in: undefined })]; },
			{ reconnect: async () => { answered = true; return reply({ id: 'figma', state: 'working', state_since: 't', tool_count: 3, failure_class: null }); } },
		);
		await openRow('Figma');
		button('Reconnect')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('Connected · 3 actions'));
		// The row still has its name, its reach and its buttons: nothing froze.
		expect(host.querySelector('[data-settings-row="Figma"]')).not.toBeNull();
		await vi.waitFor(() => expect(button('Reconnect')!.disabled).toBe(false));
		expect(button('Remove')!.disabled).toBe(false);
	});

	it('leaves Remove pressable while Reconnect is still working', async () => {
		const fetch = stubApi(() => [figma({ state: 'working', sign_in: undefined })], { reconnect: () => new Promise(() => {}) });
		await openRow('Figma');
		button('Reconnect')!.click();
		flushSync();
		expect(button('Reconnect')!.disabled).toBe(true);
		expect(button('Remove')!.disabled).toBe(false);
		expect(button('Remove')!.classList.contains('loading')).toBe(false);
		button('Remove')!.click();
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers/figma'), expect.objectContaining({ method: 'DELETE' })));
		await vi.waitFor(() => expect(host.querySelector('[data-settings-row="Figma"]')).toBeNull());
	});

	it('spins only the Remove button while Remove is working, and not Reconnect', async () => {
		stubApi(() => [figma({ state: 'working', sign_in: undefined })], { remove: () => new Promise(() => {}) });
		await openRow('Figma');
		button('Remove')!.click();
		flushSync();
		expect(button('Remove')!.disabled).toBe(true);
		expect(button('Reconnect')!.classList.contains('loading')).toBe(false);
	});
});
