import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import ExternalProviderSettings from './ExternalProviderSettings.svelte';

const server = {
	id: 'ns-external', provider: 'external', instance_id: 'provider.example/v1',
	instance_name: 'External', kind: 'external' as const, dialect: 'openai' as const,
	port: 443, status: 'online' as const, source: 'manual' as const,
	latency_ms: null, memory_total_mb: null, memory_used_mb: null, gpu_info: null,
	endpoints: [], active_host: 'provider.example', api_base: null, mgmt_base: null,
	has_key: true, metadata: { base_url: 'https://provider.example/v1' },
};

describe('ExternalProviderSettings', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
		vi.unstubAllGlobals();
	});

	it('shows the saved-key placeholder and submits a typed replacement key', async () => {
		const fetch = vi.fn(async () => new Response(JSON.stringify({ detail: 'probe failed' }), { status: 400 }));
		vi.stubGlobal('fetch', fetch);
		component = mount(ExternalProviderSettings, { target: document.body, props: { server } });
		flushSync();

		const key = document.querySelector('input[type="password"]') as HTMLInputElement;
		expect(key.placeholder).toContain('saved; replace');
		key.value = 'replacement-key';
		key.dispatchEvent(new Event('input', { bubbles: true }));
		(document.querySelector('button') as HTMLButtonElement).click();
		await Promise.resolve();
		await Promise.resolve();

		expect(fetch).toHaveBeenCalledTimes(1);
		const request = fetch.mock.calls[0][1] as RequestInit;
		expect(JSON.parse(request.body as string)).toMatchObject({
			base_url: 'https://provider.example/v1', dialect: 'openai', api_key: 'replacement-key',
		});
	});
});

describe('ExternalProviderSettings declared model ids', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
		vi.unstubAllGlobals();
	});

	async function saveWith(text: string | null) {
		const fetch = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify({ detail: 'probe failed' }), { status: 400 }));
		vi.stubGlobal('fetch', fetch);
		const declared = { ...server, metadata: { ...server.metadata, declared_models: ['glm-5.3', 'deepseek-v4.1-flash'] } };
		component = mount(ExternalProviderSettings, { target: document.body, props: { server: declared } });
		flushSync();
		const ids = document.querySelector('textarea[aria-label="Model IDs"]') as HTMLTextAreaElement;
		expect(ids.value).toBe('glm-5.3\ndeepseek-v4.1-flash');
		if (text !== null) {
			ids.value = text;
			ids.dispatchEvent(new Event('input', { bubbles: true }));
		}
		(document.querySelector('button') as HTMLButtonElement).click();
		await Promise.resolve();
		await Promise.resolve();
		return JSON.parse((fetch.mock.calls[0][1] as RequestInit).body as string);
	}

	it('shows the saved ids and sends them back unchanged', async () => {
		expect((await saveWith(null)).model_ids).toEqual(['glm-5.3', 'deepseek-v4.1-flash']);
	});

	it('sends an emptied field as an empty list, which clears the ids', async () => {
		expect((await saveWith('')).model_ids).toEqual([]);
	});
});

describe('ExternalProviderSettings billing', () => {
	let component: ReturnType<typeof mount> | null = null;
	afterEach(() => { if (component) unmount(component); component = null; document.body.innerHTML = ''; vi.unstubAllGlobals(); });
	const label = (name: string) => [...document.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent?.trim() === name)!;
	const settle = async () => { await new Promise((resolve) => setTimeout(resolve, 0)); flushSync(); };

	it('shows an unset endpoint as not recorded and saves a chosen mode with its revision', async () => {
		const fetch = vi.fn(async () => new Response(JSON.stringify({ id: 'ns-external', billing: { mode: 'pay_per_use', monthly_price_cents: null, charge_day: null }, billing_revision: 8, list_priced: true }), { status: 200 }));
		vi.stubGlobal('fetch', fetch);
		component = mount(ExternalProviderSettings, { target: document.body, props: { server: { ...server, billing: null, billing_revision: 7, list_priced: true } } });
		flushSync();
		expect(document.body.textContent).toContain('Price not recorded');
		label('Set price').click(); flushSync();
		label('Pay per use').click(); flushSync();
		label('Save').click(); await settle();
		const calls = fetch.mock.calls as unknown as [string, RequestInit][];
		expect(calls[0][0]).toContain('/api/settings/network-servers/ns-external/billing');
		expect(JSON.parse(calls[0][1].body as string)).toEqual({ mode: 'pay_per_use', monthly_price_cents: null, charge_day: null, expected_billing_revision: 7 });
		expect(calls.some(([url]) => /\/api\/settings\/network-servers\?/.test(url)), 'the open card reloads its server list after a billing save').toBe(true);
	});

	it('shows the server text when an endpoint billing save fails, keeps the editor open and does not reload', async () => {
		const fetch = vi.fn(async () => new Response(JSON.stringify({ detail: 'Could not save billing.' }), { status: 500 }));
		vi.stubGlobal('fetch', fetch);
		component = mount(ExternalProviderSettings, { target: document.body, props: { server: { ...server, billing: null, billing_revision: 7, list_priced: true } } });
		flushSync();
		label('Set price').click(); flushSync();
		label('Pay per use').click(); flushSync();
		label('Save').click(); await settle();
		expect(document.body.textContent).toContain('Could not save billing.');
		expect(label('Pay per use').getAttribute('aria-checked')).toBe('true');
		expect(fetch).toHaveBeenCalledTimes(1);
	});

	it('reads a recorded subscription like an account row', () => {
		component = mount(ExternalProviderSettings, { target: document.body, props: { server: { ...server, billing: { mode: 'subscription' as const, monthly_price_cents: 3000, charge_day: 21 }, billing_revision: 1 } } });
		flushSync();
		expect(document.body.textContent).toContain('$30.00 a month · charged on the 21st');
	});
});
