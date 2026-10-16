import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import OwnOmlxManagement from './OwnOmlxManagement.svelte';
import type { OwnOmlxManagement as Management } from '$lib/conductorSources';

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const RESTORES = ['Max Concurrent Requests to 4', 'Ctx Window for Qwen3.8-27B-oQ4e-mtp to oMLX default'];

let component: ReturnType<typeof mount> | null = null;
let management: Management | null;
let answer: Response;
let posted: string[];
let onResult: Mock<(message: string, error: boolean) => void>;

beforeEach(() => {
	posted = [];
	onResult = vi.fn<(message: string, error: boolean) => void>();
	answer = json({ ok: true });
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		if (init?.method === 'POST') { posted.push(url); return answer; }
		return json({ sources: [{ source: 'omlx', name: 'oMLX', state: 'ready', omlx_management: management }] });
	}));
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
});

async function render(state: Management | null) {
	management = state;
	component = mount(OwnOmlxManagement, { target: document.body, props: { onResult } });
	flushSync();
	await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
	await tick(); await new Promise((done) => setTimeout(done)); await tick();
}
const button = (label: string) => [...document.querySelectorAll('button')].find((item) => item.textContent?.trim() === label);
const managed = (restores = RESTORES, forgetsKey = true): Management => ({ state: 'managed', cannot_check: null, restores, forgets_key: forgetsKey });

describe('OwnOmlxManagement', () => {
	it('shows nothing for the oMLX CORSIA installed', async () => {
		await render(null);
		expect(document.querySelectorAll('button')).toHaveLength(0);
	});

	it('names every value Stop managing puts back, then stops after confirming', async () => {
		await render(managed());
		button('Stop managing')!.click();
		flushSync();
		const dialog = document.querySelector('[role="alertdialog"]')!;
		expect(dialog.textContent).toContain('Stop managing your oMLX?');
		expect(dialog.textContent).toContain(
			'CORSIA puts back Max Concurrent Requests to 4; Ctx Window for Qwen3.8-27B-oQ4e-mtp to oMLX default. Then CORSIA forgets the admin key it stored. '
			+ 'To keep using your oMLX in CORSIA, enter that key again in CORSIA.');
		expect(posted).toEqual([]);

		management = { state: 'not_managed', cannot_check: null, restores: [], forgets_key: false };
		[...dialog.querySelectorAll('button')].find((item) => item.textContent?.trim() === 'Stop managing')!.click();
		await vi.waitFor(() => expect(onResult).toHaveBeenCalled());
		expect(posted).toEqual(['/api/providers/sources/omlx/management/stop']);
		expect(onResult).toHaveBeenCalledWith('Your oMLX’s values are back, and CORSIA no longer changes its settings.', false);
		await vi.waitFor(() => expect(button('Let CORSIA manage it')).toBeTruthy());
	});

	it('says nothing needs putting back, and keeps a key the person stored', async () => {
		await render(managed([], false));
		button('Stop managing')!.click();
		flushSync();
		expect(document.querySelector('[role="alertdialog"]')?.textContent).toContain('CORSIA hasn’t changed any setting, so nothing needs putting back.');
		expect(document.querySelector('[role="alertdialog"]')?.textContent).not.toContain('admin key');
	});

	it('cancelling Stop managing changes nothing', async () => {
		await render(managed());
		button('Stop managing')!.click();
		flushSync();
		button('Cancel')!.click();
		await tick(); await new Promise((done) => setTimeout(done));
		expect(posted).toEqual([]);
		expect(onResult).not.toHaveBeenCalled();
	});

	it('keeps a failed restore visible with Try again', async () => {
		await render(managed());
		answer = json({ detail: 'CORSIA couldn’t put your oMLX’s values back.' }, 502);
		management = { ...managed(), state: 'restore_failed' };
		button('Stop managing')!.click();
		flushSync();
		[...document.querySelectorAll<HTMLButtonElement>('[role="alertdialog"] button')].find((item) => item.textContent?.trim() === 'Stop managing')!.click();
		await vi.waitFor(() => expect(onResult).toHaveBeenCalledWith('CORSIA couldn’t put your oMLX’s values back.', true));
		await vi.waitFor(() => expect(document.querySelector('[role="status"]')?.textContent).toBe('CORSIA couldn’t put your oMLX’s values back.'));

		answer = json({ ok: true });
		button('Try again')!.click();
		await vi.waitFor(() => expect(posted).toHaveLength(2));
		expect(posted[1]).toBe('/api/providers/sources/omlx/management/stop');
	});

	it('gives Welcome’s consent before Let CORSIA manage it starts', async () => {
		await render({ state: 'not_managed', cannot_check: null, restores: [], forgets_key: false });
		answer = json({ ok: true, in_progress: true });
		button('Let CORSIA manage it')!.click();
		flushSync();
		expect(document.querySelector('[role="alertdialog"]')?.textContent).toContain(
			'Letting CORSIA manage it means CORSIA reads your oMLX’s admin key, never changing it, and changes these settings for every app that uses this oMLX');
		expect(posted).toEqual([]);
		[...document.querySelectorAll<HTMLButtonElement>('[role="alertdialog"] button')].find((item) => item.textContent?.trim() === 'Let CORSIA manage it')!.click();
		await vi.waitFor(() => expect(onResult).toHaveBeenCalledWith('CORSIA is setting up your oMLX. It asks in chat before it changes any setting.', false));
		expect(posted).toEqual(['/api/providers/sources/omlx/management/start']);
	});
});
