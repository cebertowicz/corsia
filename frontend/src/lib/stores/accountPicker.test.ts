import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { loadPickerPolicy, pickerAccounts, pickerPolicy, pickerPolicyProject, policyFor, vendorRows } from './accountPicker';
import { token } from './auth';
import type { Model } from './models';
import type { RunsOn } from './modelTurn';
import type { VendorAccount } from './vendorAccounts';

function account(id: string, over: Partial<VendorAccount> = {}, provider = 'claude-code'): VendorAccount {
	return {
		id, provider, name: id, is_default: false, state: 'signed_in', revision: 1, identity: {},
		label: { name: id, email: null, text: id, custom: null },
		usage: { state: 'allowed', usable: true, windows: [], checking: false, check: null },
		...over,
	};
}

function entry(accountId: string, over: Partial<RunsOn> = {}): RunsOn {
	return {
		account_id: accountId, selected: false, status: 'ready', status_reason: null, resets_at: null,
		usage: { state: 'allowed', usable: true, windows: [] }, ...over,
	};
}

function model(id: string, runsOn: RunsOn[], connection = 'claude-code'): Model {
	return {
		id, name: id, size: '', backend: 'cc', provider: connection, connection, speed: '', status: 'ready',
		litellm_model: `${connection}/${id}`, runs_on: runsOn,
	} as Model;
}

const WORK = account('work');
const PERSONAL = account('personal');
const TWO_ACCOUNT_CATALOG = [
	model('opus', [entry('work', { selected: true }), entry('personal')]),
	model('sonnet', [entry('work', { selected: true }), entry('personal')]),
];

describe('vendorRows', () => {
	it('lists the vendor\'s signed-in accounts the catalog says a row runs on, and no other', () => {
		const other = account('elsewhere', {}, 'codex');
		const unpermitted = account('unpermitted');
		expect(vendorRows('claude-code', TWO_ACCOUNT_CATALOG, [WORK, PERSONAL, other, unpermitted]).map((row) => row.id)).toEqual(['work', 'personal']);
	});

	it('never lists a removed account', () => {
		expect(vendorRows('claude-code', TWO_ACCOUNT_CATALOG, [WORK, account('personal', { state: 'removed' })]).map((row) => row.id)).toEqual(['work']);
	});
});

describe('pickerAccounts', () => {
	it('has no rows for a vendor with fewer than two accounts', () => {
		expect(pickerAccounts('claude-code', [model('opus', [entry('work', { selected: true })])], [WORK], undefined)).toEqual([]);
		expect(pickerAccounts('claude-code', TWO_ACCOUNT_CATALOG, [WORK, account('personal', { state: 'removed' })], undefined)).toEqual([]);
	});

	it('shows the one account left, unmarked, when the catalog selects none (the binding was removed or turned off)', () => {
		const lost = [model('opus', [entry('work')]), model('sonnet', [entry('work')])];
		const rows = pickerAccounts('claude-code', lost, [WORK, account('gone', { state: 'removed' })], undefined);
		expect(rows.map((row) => [row.account.id, row.marked, row.choosable])).toEqual([['work', false, true]]);
	});

	it('shows no rows for a lone account the catalog does select, or when none is left', () => {
		expect(pickerAccounts('claude-code', [model('opus', [entry('work', { selected: true })])], [WORK], undefined)).toEqual([]);
		expect(pickerAccounts('claude-code', [model('opus', [])], [WORK], undefined)).toEqual([]);
	});

	it('marks exactly the account of the selected runs_on entry', () => {
		const rows = pickerAccounts('claude-code', TWO_ACCOUNT_CATALOG, [WORK, PERSONAL], undefined);
		expect(rows.map((row) => [row.account.id, row.marked])).toEqual([['work', true], ['personal', false]]);
		const moved = [model('opus', [entry('work'), entry('personal', { selected: true })])];
		expect(pickerAccounts('claude-code', moved, [WORK, PERSONAL], undefined).map((row) => row.marked)).toEqual([false, true]);
	});

	it('marks the current model\'s selected account over another model\'s', () => {
		const catalog = [
			model('opus', [entry('work', { selected: true }), entry('personal')]),
			model('sonnet', [entry('work'), entry('personal', { selected: true })]),
		];
		expect(pickerAccounts('claude-code', catalog, [WORK, PERSONAL], undefined, 'claude-code/sonnet').map((row) => row.marked)).toEqual([false, true]);
	});

	it('numbers the rows in rollover order only while the scope\'s rollover is on', () => {
		const off = pickerAccounts('claude-code', TWO_ACCOUNT_CATALOG, [WORK, PERSONAL], { rollover: false, order: ['personal', 'work'] });
		expect(off.map((row) => [row.account.id, row.order])).toEqual([['work', null], ['personal', null]]);
		const on = pickerAccounts('claude-code', TWO_ACCOUNT_CATALOG, [WORK, PERSONAL], { rollover: true, order: ['personal', 'work'] });
		expect(on.map((row) => [row.account.id, row.order])).toEqual([['personal', 1], ['work', 2]]);
	});

	it('cannot choose a signed-out or limited account, and can choose every other', () => {
		const limited = account('personal', { usage: { state: 'limited', usable: false, windows: [], checking: false, check: null } });
		expect(pickerAccounts('claude-code', TWO_ACCOUNT_CATALOG, [WORK, limited], undefined).map((row) => row.choosable)).toEqual([true, false]);
		const signedOut = account('personal', { state: 'signed_out' });
		expect(pickerAccounts('claude-code', TWO_ACCOUNT_CATALOG, [WORK, signedOut], undefined).map((row) => row.choosable)).toEqual([true, false]);
	});

	it('keeps the marked account in the list even when it is limited', () => {
		const limited = account('work', { usage: { state: 'limited', usable: false, windows: [], checking: false, check: null } });
		const rows = pickerAccounts('claude-code', TWO_ACCOUNT_CATALOG, [limited, PERSONAL], undefined);
		expect(rows.map((row) => [row.account.id, row.marked, row.choosable])).toEqual([['work', true, false], ['personal', false, true]]);
	});
});

describe('loadPickerPolicy', () => {
	const fetchMock = vi.fn();
	beforeEach(() => {
		token.set('synthetic');
		pickerPolicy.set({});
		pickerPolicyProject.set(undefined);
		fetchMock.mockReset();
		vi.stubGlobal('fetch', fetchMock);
	});
	afterEach(() => {
		vi.unstubAllGlobals();
		token.set(null);
		pickerPolicy.set({});
	});

	const policy = (providers: unknown[]) => new Response(JSON.stringify({ scope: 'project', providers }), { status: 200, headers: { 'Content-Type': 'application/json' } });
	const provider = (key: string, over: Record<string, unknown> = {}) => ({
		provider_key: key, capabilities: { units: 'accounts', rollover: true }, rollover: true, order: ['b', 'a'], disabled: [], revision: 1,
		units: [{ id: 'a', label: 'a', enabled: true }, { id: 'b', label: 'b', enabled: true }], ...over,
	});

	it('reads the effective policy of the thread\'s project, per vendor', async () => {
		fetchMock.mockResolvedValueOnce(policy([provider('claude-code'), provider('codex', { rollover: false, order: null })]));
		await loadPickerPolicy('project-1');
		expect(String(fetchMock.mock.calls[0][0])).toBe('/api/provider-policy?project_id=project-1');
		expect(get(pickerPolicy)).toEqual({
			'claude-code': { rollover: true, order: ['b', 'a'] },
			codex: { rollover: false, order: ['a', 'b'] },
		});
	});

	it('reads the workspace policy for a thread with no project', async () => {
		fetchMock.mockResolvedValueOnce(policy([]));
		await loadPickerPolicy(null);
		expect(String(fetchMock.mock.calls[0][0])).toBe('/api/provider-policy');
		expect(get(pickerPolicyProject)).toBeNull();
	});

	it('drops a read that a newer one superseded, whatever order they resolve in', async () => {
		let first!: (response: Response) => void;
		fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { first = resolve; }));
		fetchMock.mockResolvedValueOnce(policy([provider('claude-code', { order: ['b'] })]));
		const older = loadPickerPolicy('project-1');
		await loadPickerPolicy('project-2');
		first(policy([provider('claude-code', { order: ['a'] })]));
		await older;
		expect(get(pickerPolicyProject)).toBe('project-2');
		expect(get(pickerPolicy)['claude-code'].order).toEqual(['b']);
	});

	it('hands a vendor\'s policy back only for the project it was read for', async () => {
		fetchMock.mockResolvedValueOnce(policy([provider('claude-code')]));
		await loadPickerPolicy('project-1');
		expect(get(policyFor)('project-1', 'claude-code')?.order).toEqual(['b', 'a']);
		expect(get(policyFor)('project-2', 'claude-code')).toBeUndefined();
		expect(get(policyFor)(null, 'claude-code')).toBeUndefined();
	});

	it('records the project it read, and keeps the last read when a read fails', async () => {
		fetchMock.mockResolvedValueOnce(policy([provider('claude-code')]));
		await loadPickerPolicy('project-1');
		expect(get(pickerPolicyProject)).toBe('project-1');
		fetchMock.mockResolvedValueOnce(new Response('{}', { status: 500 }));
		await loadPickerPolicy('project-2');
		expect(get(pickerPolicyProject)).toBe('project-1');
	});

	it('skips a vendor whose units are not accounts or that cannot roll over', async () => {
		fetchMock.mockResolvedValueOnce(policy([
			provider('local', { capabilities: { units: 'self', rollover: false } }),
			provider('external', { capabilities: { units: 'models', rollover: false } }),
		]));
		await loadPickerPolicy('project-1');
		expect(get(pickerPolicy)).toEqual({});
	});

	it('keeps the last good policy when the read fails', async () => {
		pickerPolicy.set({ 'claude-code': { rollover: true, order: ['a'] } });
		fetchMock.mockResolvedValueOnce(new Response('{}', { status: 500 }));
		await loadPickerPolicy('project-1');
		expect(get(pickerPolicy)).toEqual({ 'claude-code': { rollover: true, order: ['a'] } });
	});
});
