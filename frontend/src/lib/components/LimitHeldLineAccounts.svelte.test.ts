import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/stores/threadAccountPin', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/threadAccountPin')>()),
	pinThreadAccount: vi.fn(),
}));

import LimitHeldLine from './LimitHeldLine.svelte';
import { token } from '$lib/stores/auth';
import { pickerPolicy, pickerPolicyProject } from '$lib/stores/accountPicker';
import type { Model } from '$lib/stores/models';
import type { RunsOn } from '$lib/stores/modelTurn';
import { pinThreadAccount, TURN_RUNNING } from '$lib/stores/threadAccountPin';
import { activeThreadId, threads } from '$lib/stores/threads';
import { vendorAccounts, type VendorAccount } from '$lib/stores/vendorAccounts';

const RESET = '2099-01-02T16:37:00Z';

function account(id: string, text: string): VendorAccount {
	return {
		id, provider: 'claude-code', name: id, is_default: false, state: 'signed_in', revision: 1, identity: {},
		label: { name: text.split(' · ')[0], email: null, text, custom: null },
	};
}

const entry = (accountId: string, over: Partial<RunsOn> = {}): RunsOn => ({
	account_id: accountId, selected: false, status: 'ready', status_reason: null, resets_at: null,
	usage: { state: 'allowed', usable: true, windows: [] }, ...over,
});
const limited = (accountId: string, over: Partial<RunsOn> = {}): RunsOn => entry(accountId, {
	status: 'unavailable', status_reason: 'usage_limited', resets_at: RESET,
	usage: { state: 'limited', usable: false, reason: 'five_hour_limit', resets_at: RESET, windows: [] }, ...over,
});
const model = (entries: RunsOn[]): Model => ({
	id: 'claude-code/opus', name: 'Opus', connection: 'claude-code', size: '', backend: 'harness', speed: '', status: 'ready', runs_on: entries,
} as Model);

function readPolicy(project: string | null, order: string[]) {
	pickerPolicy.set({ 'claude-code': { rollover: true, order } });
	pickerPolicyProject.set(project);
}

const PERSONAL = account('personal', 'Personal Max · sam@example.com');
const ACME = account('acme', 'Acme (Team) · sam@example.com');

let component: Record<string, unknown> | null = null;
const fetchMock = vi.fn();
const buttons = () => [...document.querySelectorAll('button')].map((b) => b.textContent?.trim());
const text = () => document.querySelector('.limit-held-text')?.textContent;
const settle = async () => { await new Promise((resolve) => setTimeout(resolve, 10)); flushSync(); };

function render(row: Model, onSwitchModel = vi.fn()) {
	component = mount(LimitHeldLine, { target: document.body, props: { model: row, onSwitchModel } }) as Record<string, unknown>;
	flushSync();
	return onSwitchModel;
}

beforeEach(() => {
	token.set('test-token');
	activeThreadId.set('thread-1');
	threads.set([{ id: 'thread-1', title: 'Held', project_id: 'project-1' } as never]);
	vendorAccounts.set([PERSONAL, ACME]);
	pickerPolicy.set({});
	pickerPolicyProject.set(undefined);
	vi.stubGlobal('fetch', fetchMock.mockImplementation(async () => new Response('{}', { status: 404 })));
	vi.mocked(pinThreadAccount).mockReset();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
	fetchMock.mockReset();
	token.set(null);
	activeThreadId.set(null);
	threads.set([]);
	vendorAccounts.set([]);
	pickerPolicy.set({});
	pickerPolicyProject.set(undefined);
});

describe('held line: continue on another account', () => {
	const row = () => model([limited('personal', { selected: true }), entry('acme')]);

	it('names the limited account and replaces "Switch model" with one action', () => {
		const onSwitchModel = render(row());
		expect(text()).toMatch(/^Personal Max · sam@example\.com is at its limit · back /);
		expect(buttons()).toEqual(['Continue on Acme (Team) · sam@example.com']);
		expect(onSwitchModel).not.toHaveBeenCalled();
	});

	it('pins the thread through the picker\'s own path, once, and stays one line', async () => {
		vi.mocked(pinThreadAccount).mockResolvedValue({ ok: true, changed: true });
		readPolicy('project-1', ['personal', 'acme']);
		render(row());
		document.querySelector('button')!.click();
		await settle();
		expect(pinThreadAccount).toHaveBeenCalledExactlyOnceWith('thread-1', 'acme');
		expect(fetchMock).not.toHaveBeenCalled();
		expect(document.querySelectorAll('.limit-held')).toHaveLength(1);
		expect(document.querySelector('[role="alert"]')).toBeNull();
	});

	it('shows the pin\'s refusal inside the line, and takes another try', async () => {
		vi.mocked(pinThreadAccount).mockResolvedValue({ ok: false, message: TURN_RUNNING });
		render(row());
		document.querySelector('button')!.click();
		await settle();
		expect(document.querySelector('.limit-held [role="alert"]')?.textContent).toBe(TURN_RUNNING);
		expect(document.querySelector<HTMLButtonElement>('button')!.disabled).toBe(false);
	});

	it('keeps today\'s line, with "Switch model", on a draft chat that has no thread to move', () => {
		activeThreadId.set(null);
		const onSwitchModel = render(row());
		expect(text()).toMatch(/^Usage limit reached · resets /);
		expect(buttons()).toEqual(['Switch model']);
		document.querySelector('button')!.click();
		expect(onSwitchModel).toHaveBeenCalledOnce();
	});

	it('reads the accounts once when they are not loaded yet', async () => {
		vendorAccounts.set([]);
		readPolicy('project-1', ['personal', 'acme']);
		render(row());
		await settle();
		expect(fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/vendor-accounts'))).toHaveLength(1);
		expect(fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/provider-policy'))).toHaveLength(0);
	});

	it('reads the rollover order when the accounts are already loaded but the policy is not', async () => {
		const third = account('third', 'Third · sam@example.com');
		vendorAccounts.set([PERSONAL, ACME, third]);
		fetchMock.mockImplementation(async (url: unknown) => String(url).startsWith('/api/provider-policy')
			? new Response(JSON.stringify({ providers: [{
				provider_key: 'claude-code', capabilities: { units: 'accounts', rollover: true },
				rollover: true, order: ['third', 'personal', 'acme'], units: [],
			}] }), { status: 200 })
			: new Response('{}', { status: 404 }));
		render(model([limited('personal', { selected: true }), entry('acme'), entry('third')]));
		expect(buttons()).toEqual(['Continue on Acme (Team) · sam@example.com']);
		await settle();
		const policyCalls = fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/provider-policy'));
		expect(policyCalls.map(([url]) => String(url))).toEqual(['/api/provider-policy?project_id=project-1']);
		expect(fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/vendor-accounts'))).toHaveLength(0);
		expect(buttons()).toEqual(['Continue on Third · sam@example.com']);
	});

	it('reads the thread\'s own project policy when the loaded one belongs to another project', async () => {
		vendorAccounts.set([PERSONAL, ACME, account('third', 'Third · sam@example.com')]);
		readPolicy('project-x', ['personal', 'acme', 'third']);
		fetchMock.mockImplementation(async (url: unknown) => String(url).startsWith('/api/provider-policy')
			? new Response(JSON.stringify({ providers: [{
				provider_key: 'claude-code', capabilities: { units: 'accounts', rollover: true },
				rollover: true, order: ['third', 'acme', 'personal'], units: [],
			}] }), { status: 200 })
			: new Response('{}', { status: 404 }));
		render(model([limited('personal', { selected: true }), entry('acme'), entry('third')]));
		await settle();
		expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual(['/api/provider-policy?project_id=project-1']);
		expect(buttons()).toEqual(['Continue on Third · sam@example.com']);
	});

	it('uses the newest policy read when an earlier project\'s read lands last', async () => {
		vendorAccounts.set([PERSONAL, ACME, account('third', 'Third · sam@example.com')]);
		threads.set([
			{ id: 'thread-1', title: 'Held', project_id: 'project-1' } as never,
			{ id: 'thread-2', title: 'Held 2', project_id: 'project-2' } as never,
		]);
		const reply = (order: string[]) => new Response(JSON.stringify({ providers: [{
			provider_key: 'claude-code', capabilities: { units: 'accounts', rollover: true }, rollover: true, order, units: [],
		}] }), { status: 200 });
		const pending: Record<string, (response: Response) => void> = {};
		fetchMock.mockImplementation((url: unknown) => String(url).startsWith('/api/provider-policy')
			? new Promise<Response>((resolve) => { pending[String(url).split('=')[1]] = resolve; })
			: Promise.resolve(new Response('{}', { status: 404 })));
		render(model([limited('personal', { selected: true }), entry('acme'), entry('third')]));
		await settle();
		activeThreadId.set('thread-2');
		await settle();
		expect(Object.keys(pending)).toEqual(['project-1', 'project-2']);
		pending['project-2'](reply(['third', 'acme', 'personal']));
		await settle();
		pending['project-1'](reply(['personal', 'acme', 'third']));
		await settle();
		expect(buttons()).toEqual(['Continue on Third · sam@example.com']);
	});

	it('clears a refusal when the active thread changes', async () => {
		vi.mocked(pinThreadAccount).mockResolvedValue({ ok: false, message: TURN_RUNNING });
		readPolicy('project-1', ['personal', 'acme']);
		render(row());
		document.querySelector('button')!.click();
		await settle();
		expect(document.querySelector('[role="alert"]')).not.toBeNull();
		activeThreadId.set('thread-2');
		await settle();
		expect(document.querySelector('[role="alert"]')).toBeNull();
	});
});

describe('held line: every account is at its limit', () => {
	it('says so with no action, not even "Switch model"', () => {
		render(model([limited('personal', { selected: true }), limited('acme')]));
		expect(text()).toMatch(/^All Claude accounts are at their limit · first back /);
		expect(buttons()).toEqual([]);
	});
});
