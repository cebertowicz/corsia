import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/ws/connection', () => ({ disconnect: vi.fn() }));

import SettingsKeys from './SettingsKeys.svelte';
import { token, user } from '$lib/stores/auth';
import { connectionError } from '$lib/stores/connection';
import { KEYS_SIGNED_OUT } from '$lib/stores/keys';
import { disconnect } from '$lib/ws/connection';

const VALUE = 'BSAmarkup-test-value-0123';
const PASSWORD = 'typed-in-the-form';

const webSearch = {
	name: 'web-search', label: 'Web search', description: '', builtin: true, added_by: null, used_by: 'web search',
	is_set: false, source: null, set_at: null, checkable: true,
};
const maps = {
	name: 'maps', label: 'Maps', description: 'Map tiles', builtin: false, added_by: 'Conductor', used_by: null,
	is_set: false, source: null, set_at: null, checkable: false,
};

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let entries: Record<string, unknown>[];
let writeAnswer: { status: number; body: unknown };
let checkAnswer: unknown;
let fetchMock: ReturnType<typeof vi.fn>;

function json(status: number, body: unknown) {
	return { ok: status < 400, status, json: async () => body } as Response;
}

beforeEach(() => {
	token.set('session-token');
	user.set({ id: 'owner', name: 'Owner' });
	connectionError.set(null);
	entries = [{ ...webSearch }, { ...maps }];
	writeAnswer = { status: 200, body: { key: webSearch } };
	checkAnswer = { outcome: 'works', reason: null };
	fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
		if (url === '/api/keys') return json(200, { keys: entries });
		if (url.endsWith('/check')) return json(200, checkAnswer);
		return json(writeAnswer.status, writeAnswer.body);
	});
	vi.stubGlobal('fetch', fetchMock);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	vi.clearAllMocks();
	token.set(null);
	user.set(null);
	connectionError.set(null);
});

async function mountKeys() {
	component = mount(SettingsKeys, { target: host });
	await vi.waitFor(() => expect(host.querySelector('[data-key-entry="web-search"]')).not.toBeNull());
}

function button(card: string, label: string): HTMLButtonElement {
	const found = [...host.querySelectorAll<HTMLButtonElement>(`[data-key-entry="${card}"] button`)]
		.find((b) => b.textContent?.trim() === label);
	if (!found) throw new Error(`no ${label} button on ${card}`);
	return found;
}

function inputs(card: string) {
	const form = host.querySelector(`[data-key-entry="${card}"] form`)!;
	return {
		form,
		value: form.querySelector<HTMLInputElement>('input[aria-label="Key"]'),
		password: form.querySelector<HTMLInputElement>('input[aria-label="CORSIA password"]')!,
	};
}

function type(input: HTMLInputElement, text: string) {
	input.value = text;
	input.dispatchEvent(new Event('input', { bubbles: true }));
}

async function submit(card: string) {
	inputs(card).form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
	await vi.waitFor(() => expect(host.querySelector('.btn.loading, button[disabled][type="submit"]')).toBeNull());
	flushSync();
}

describe('Settings › Keys', () => {
	it('shows the help text and each row with its use or who added it', async () => {
		await mountKeys();
		expect(host.textContent).toContain(
			"Agents can use these keys but can never see them. They're stored on this Mac, and a backup of CORSIA's state folder includes them.",
		);
		const web = host.querySelector('[data-key-entry="web-search"]')!.textContent!;
		expect(web).toContain('Used by: web search');
		expect(web).toContain('Not set');
		expect(web).not.toContain('Check');
		expect(web).not.toContain('Remove');
		const added = host.querySelector('[data-key-entry="maps"]')!.textContent!;
		expect(added).toContain('Added by Conductor');
		expect(added).not.toContain('Used by');
		expect(added).toContain('Remove');
	});

	it('names where a set key comes from', async () => {
		entries = [
			{ ...webSearch, is_set: true, source: 'keys', set_at: '2026-10-08T17:00:00Z' },
			{ ...maps, name: 'a', is_set: true, source: 'settings' },
			{ ...maps, name: 'b', is_set: true, source: 'environment' },
		];
		await mountKeys();
		expect(host.textContent).toContain('Set Oct 8');
		expect(host.textContent).toContain("Set in CORSIA's settings file");
		expect(host.textContent).toContain('Set in the environment');
		expect(button('web-search', 'Replace')).toBeTruthy();
		expect(button('web-search', 'Clear')).toBeTruthy();
		expect(button('web-search', 'Check')).toBeTruthy();
	});

	it('marks the value field so nothing saves or fills it, and leaves the password field to the browser', async () => {
		await mountKeys();
		button('web-search', 'Set').click();
		flushSync();
		const { form, value, password } = inputs('web-search');
		expect(form.hasAttribute('autocomplete')).toBe(false);
		expect(value!.type).toBe('password');
		expect(value!.getAttribute('autocomplete')).toBe('off');
		for (const attr of ['data-1p-ignore', 'data-lpignore', 'data-bwignore', 'data-form-type']) {
			expect(value!.hasAttribute(attr)).toBe(true);
		}
		for (const field of [value!, password]) {
			expect(field.hasAttribute('name')).toBe(false);
			expect(field.hasAttribute('id')).toBe(false);
			expect(field.value).toBe('');
		}
		expect(password.type).toBe('password');
		expect(password.getAttribute('autocomplete')).toBe('current-password');
	});

	it('asks for both fields before sending anything', async () => {
		await mountKeys();
		button('web-search', 'Set').click();
		flushSync();
		await submit('web-search');
		expect(host.textContent).toContain('Paste the key first.');
		type(inputs('web-search').value!, VALUE);
		await submit('web-search');
		expect(host.textContent).toContain('Enter your CORSIA password.');
		expect(fetchMock.mock.calls.filter(([url]) => url !== '/api/keys')).toHaveLength(0);
	});

	it('sends the value and password in the body with the session header, and empties both fields', async () => {
		await mountKeys();
		button('web-search', 'Set').click();
		flushSync();
		writeAnswer = { status: 403, body: { detail: { code: 'password_mismatch', message: "That password didn't match. Nothing was changed." } } };
		type(inputs('web-search').value!, VALUE);
		type(inputs('web-search').password, PASSWORD);
		await submit('web-search');
		const [url, init] = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/value'))!;
		expect(url).toBe('/api/keys/web-search/value');
		expect(url).not.toContain('token');
		expect((init.headers as Record<string, string>).Authorization).toBe('Bearer session-token');
		expect(JSON.parse(init.body as string)).toEqual({ value: VALUE, password: PASSWORD });
		await vi.waitFor(() => expect(host.textContent).toContain("That password didn't match. Nothing was changed."));
		expect(inputs('web-search').value!.value).toBe('');
		expect(inputs('web-search').password.value).toBe('');
		expect(host.innerHTML).not.toContain(VALUE);
	});

	it('ends the session and shows why after the third wrong password', async () => {
		await mountKeys();
		button('web-search', 'Set').click();
		flushSync();
		writeAnswer = { status: 401, body: { detail: { code: 'signed_out', message: KEYS_SIGNED_OUT } } };
		type(inputs('web-search').value!, VALUE);
		type(inputs('web-search').password, PASSWORD);
		await submit('web-search');
		expect(get(token)).toBeNull();
		expect(get(user)).toBeNull();
		expect(get(connectionError)).toBe(KEYS_SIGNED_OUT);
		expect(disconnect).toHaveBeenCalled();
	});

	it.each([
		[{ outcome: 'works', reason: null }, 'Web search works.'],
		[{ outcome: 'key_refused', reason: null }, 'The key was refused. Check it and save it again.'],
		[{ outcome: 'failed', reason: 'network' }, "Web search didn't work just now: couldn't reach the search service."],
		[{ outcome: 'failed', reason: 'timeout' }, "Web search didn't work just now: the search service took too long."],
		[{ outcome: 'failed', reason: 'rate limit or quota' }, "Web search didn't work just now: this key has hit its usage limit."],
		[{ outcome: 'failed', reason: 'provider error' }, "Web search didn't work just now: the search service returned an error."],
	])('says what Check found: %o', async (answer, text) => {
		entries = [{ ...webSearch, is_set: true, source: 'keys', set_at: '2026-10-08T17:00:00Z' }];
		checkAnswer = answer;
		await mountKeys();
		button('web-search', 'Check').click();
		await vi.waitFor(() => expect(host.textContent).toContain(text));
	});
});
