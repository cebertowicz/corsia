import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/ws/connection', () => ({ disconnect: vi.fn() }));

import SettingsKeys from './SettingsKeys.svelte';
import { token, user } from '$lib/stores/auth';
import { connectionError } from '$lib/stores/connection';
import { KEYS_SIGNED_OUT } from '$lib/stores/keys';
import { providerCardRequest, settingsActiveSection } from '$lib/stores/settingsNavigation';
import { disconnect } from '$lib/ws/connection';

const VALUE = 'BSAmarkup-test-value-0123';
const PASSWORD = 'typed-in-the-form';

const webSearch = {
	name: 'web-search', label: 'Web search', description: '', builtin: true, added_by: null, used_by: 'web search',
	is_set: false, source: null, set_at: null, checkable: true, ignored_env_var: null,
};
const maps = {
	name: 'maps', label: 'Maps', description: 'Map tiles', builtin: false, added_by: 'Conductor', used_by: null,
	is_set: false, source: null, set_at: null, checkable: false, ignored_env_var: null,
};

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let entries: Record<string, unknown>[];
let providerKeys: Record<string, unknown>[];
let serverKeys: Record<string, unknown>[];
let mcpServers: Record<string, unknown>[];
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
	providerKeys = [];
	serverKeys = [];
	mcpServers = [];
	providerCardRequest.set(null);
	writeAnswer = { status: 200, body: { key: webSearch } };
	checkAnswer = { outcome: 'works', reason: null };
	fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
		if (url === '/api/keys') return json(200, { keys: entries, provider_keys: providerKeys, server_keys: serverKeys, mcp_servers: mcpServers });
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

	it('says on the entry card that an environment variable is ignored, and not when none is', async () => {
		const withEnv = { ...webSearch, ignored_env_var: 'BRAVE_API_KEY' };
		entries = [withEnv, maps];
		await mountKeys();
		expect(host.querySelector('[data-key-entry="web-search"] [data-ignored-env]')!.textContent)
			.toBe('BRAVE_API_KEY in the environment is ignored. Change the key here.');
		expect(host.querySelector('[data-key-entry="maps"] [data-ignored-env]')).toBeNull();
	});

	it('names when a set key was saved', async () => {
		entries = [{ ...webSearch, is_set: true, source: 'keys', set_at: '2026-10-08T17:00:00Z' }];
		await mountKeys();
		expect(host.textContent).toContain('Set Oct 8');
		expect(host.textContent).not.toContain('environment');
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

	it('shows remote no-key status accurately and submits an empty write-only MCP form to the fixed route', async () => {
		mcpServers = [
			{ server: 'local', transport: 'stdio', unsupported_references: [], keys: [{ variable: 'API_TOKEN', is_set: true, set_at: '2026-10-12T10:00:00Z', pending_restart: true, supported: true }] },
			{ server: 'remote', transport: 'remote', unsupported_references: [], keys: [] },
		];
		await mountKeys();
		const local = host.querySelector<HTMLElement>('[data-mcp-server="local"]')!;
		const remote = host.querySelector<HTMLElement>('[data-mcp-server="remote"]')!;
		expect(remote.textContent).toContain('No editable CORSIA key is configured here. Credentials managed outside CORSIA are not shown on this page.');
		expect(local.textContent).toContain('Restart the CORSIA backend to use this value for new MCP connections.');
		expect(local.textContent).not.toContain('Clear');
		expect(local.textContent).not.toContain('Remove');
		[...local.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent?.trim() === 'Replace')!.click();
		flushSync();
		const form = local.querySelector('form')!;
		const value = form.querySelector<HTMLInputElement>('input[aria-label="Key"]')!;
		const password = form.querySelector<HTMLInputElement>('input[aria-label="CORSIA password"]')!;
		type(value, VALUE);
		type(password, PASSWORD);
		form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
		await vi.waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === '/api/keys/mcp/secret/value')).toBe(true));
		const [, init] = fetchMock.mock.calls.find(([url]) => url === '/api/keys/mcp/secret/value')!;
		expect(JSON.parse((init as RequestInit).body as string)).toEqual({ server: 'local', variable: 'API_TOKEN', value: VALUE, password: PASSWORD });
		expect(value.value).toBe('');
		expect(password.value).toBe('');
		expect(host.innerHTML).not.toContain(VALUE);
	});

	describe('provider keys', () => {
		const anthropic = {
			name: 'provider-anthropic', provider: 'anthropic', label: 'Anthropic',
			is_set: true, unsupported: false, ignored_env_var: null,
		};
		const openai = {
			name: 'provider-openai', provider: 'openai', label: 'OpenAI',
			is_set: false, unsupported: false, ignored_env_var: null,
		};
		const groq = {
			name: 'provider-groq', provider: 'groq', label: 'Groq',
			is_set: true, unsupported: true, ignored_env_var: null,
		};

		function row(provider: string): HTMLElement {
			return host.querySelector<HTMLElement>(`[data-provider-key="${provider}"]`)!;
		}

		it('shows each provider as set or not set, with a link to its Providers card', async () => {
			providerKeys = [anthropic, openai];
			await mountKeys();
			expect(host.textContent).toContain('Provider keys');
			expect(row('anthropic').textContent).toContain('Set');
			expect(row('anthropic').textContent).toContain('Change in Providers');
			expect(row('openai').textContent).toContain('Not set');
			expect(row('openai').textContent).toContain('Add in Providers');
			expect(row('anthropic').querySelector('input')).toBeNull();
			[...row('openai').querySelectorAll<HTMLButtonElement>('button')]
				.find((b) => b.textContent?.trim() === 'Add in Providers')!.click();
			expect(get(providerCardRequest)).toBe('openai');
			expect(get(settingsActiveSection)).toBe('providers');
		});

		it('lists built-in keys first, then every provider as a row in one card with no heading per provider', async () => {
			providerKeys = [anthropic, openai, groq];
			await mountKeys();
			expect(host.textContent).not.toContain('Other keys');
			const subhead = [...host.querySelectorAll('h3')].find((h) => h.textContent === 'Provider keys')!;
			const webCard = host.querySelector('[data-key-entry="web-search"]')!;
			expect(webCard.compareDocumentPosition(subhead) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
			const card = row('anthropic').closest('.settings-card')!;
			expect(card.querySelectorAll('[data-provider-key]')).toHaveLength(3);
			expect(card.querySelector('h3')).toBeNull();
		});

		it('lists each server as a row in the same card as the providers, by name, with no heading of its own', async () => {
			providerKeys = [anthropic, openai];
			serverKeys = [
				{ name: 'server-ns-1', server_id: 'ns-1', label: 'Studio', is_set: true },
				{ name: 'server-ns-2', server_id: 'ns-2', label: 'Plan API', is_set: false },
			];
			await mountKeys();
			const studio = host.querySelector<HTMLElement>('[data-server-key="ns-1"]')!;
			const plan = host.querySelector<HTMLElement>('[data-server-key="ns-2"]')!;
			const card = row('anthropic').closest('.settings-card')!;
			expect(studio.closest('.settings-card')).toBe(card);
			expect(plan.closest('.settings-card')).toBe(card);
			expect(card.querySelector('h3')).toBeNull();
			expect(row('openai').compareDocumentPosition(studio) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
			expect(studio.textContent).toContain('Studio');
			expect(studio.textContent).toContain('Set');
			expect(studio.textContent).toContain('Change in Providers');
			expect(plan.textContent).toContain('Not set');
			expect(plan.textContent).toContain('Add in Providers');
			expect(studio.querySelector('input')).toBeNull();
			[...plan.querySelectorAll<HTMLButtonElement>('button')]
				.find((b) => b.textContent?.trim() === 'Add in Providers')!.click();
			expect(get(providerCardRequest)).toBe('ns-2');
			expect(get(settingsActiveSection)).toBe('providers');
		});

		it('shows the Provider keys heading for servers alone', async () => {
			serverKeys = [{ name: 'server-ns-1', server_id: 'ns-1', label: 'Studio', is_set: true }];
			await mountKeys();
			expect([...host.querySelectorAll('h3')].some((h) => h.textContent === 'Provider keys')).toBe(true);
		});

		it('says only that an environment key is ignored, since the key is changed in Providers', async () => {
			providerKeys = [{ ...anthropic, is_set: false, ignored_env_var: 'ANTHROPIC_API_KEY' }, openai];
			await mountKeys();
			expect(row('anthropic').querySelector('[data-ignored-env]')!.textContent).toBe('ANTHROPIC_API_KEY in the environment is ignored.');
			expect(row('openai').textContent).not.toContain('ignored');
		});

		it('offers Remove only for a legacy provider that still holds a key, and asks for the password', async () => {
			providerKeys = [anthropic, groq];
			await mountKeys();
			expect(row('anthropic').textContent).not.toContain('Remove');
			expect(row('groq').textContent).not.toContain('in Providers');
			const remove = [...row('groq').querySelectorAll<HTMLButtonElement>('button')]
				.find((b) => b.textContent?.trim() === 'Remove')!;
			remove.click();
			flushSync();
			const form = row('groq').querySelector('form')!;
			expect(form.querySelector('input[aria-label="Key"]')).toBeNull();
			type(form.querySelector<HTMLInputElement>('input[aria-label="CORSIA password"]')!, PASSWORD);
			form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
			await vi.waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/remove'))).toBe(true));
			const [url] = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/remove'))!;
			expect(url).toBe('/api/keys/provider-groq/remove');
		});
	});
});
