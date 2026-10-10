import { describe, expect, it } from 'vitest';
import type { Model } from './models';
import { acceptsSend, accountTurn, modelAvailability, type RunsOn } from './modelTurn';

const usage = { state: 'limited' as const, usable: false, windows: [] };
const selected: RunsOn = {
	account_id: 'a', selected: true, status: 'unavailable', status_reason: 'usage_limited', resets_at: '2099-01-01T00:00:00Z', usage,
};
const other: RunsOn = { account_id: 'b', selected: false, status: 'ready', status_reason: null, resets_at: null, usage: { ...usage, state: 'allowed', usable: true } };
const row = (over: Partial<Model> = {}): Model => ({ id: 'm', name: 'M', size: '', backend: 'cc', speed: '', status: 'ready', runs_on: [selected, other], ...over });

describe('modelAvailability', () => {
	it('reads availability from the row\'s runnable and the reset and usage from the selected account', () => {
		const turn = modelAvailability(row({ runnable: { status: 'ready', status_reason: null } }));
		expect(turn.status).toBe('ready');
		expect(turn.status_reason).toBeNull();
		expect(turn.resets_at).toBe('2099-01-01T00:00:00Z');
		expect(turn.usage).toBe(selected.usage);
	});

	it('takes a block from runnable, with its reason', () => {
		expect(modelAvailability(row({ runnable: { status: 'unavailable', status_reason: 'account_model_not_discovered' } })))
			.toMatchObject({ status: 'unavailable', status_reason: 'account_model_not_discovered' });
	});

	it('falls back to the selected account without runnable, as an older server or a local row reads', () => {
		expect(modelAvailability(row())).toBe(selected);
		expect(modelAvailability(row({ runs_on: undefined, status: 'unavailable', status_reason: 'x' }))).toMatchObject({ status: 'unavailable', status_reason: 'x' });
	});

	it('leaves the account\'s own decision to accountTurn, whatever runnable says', () => {
		expect(accountTurn(row({ runnable: { status: 'ready', status_reason: null } }))).toBe(selected);
	});
});

describe('acceptsSend', () => {
	// The one rule the composer's send gate and its chip share: `runnable` decides availability, and a
	// model blocked only by a usage limit still accepts a held send.
	const blocked = (status_reason: string) => ({ status: 'unavailable' as const, status_reason });

	it('takes a send for an available model, whatever the selected account says', () => {
		expect(acceptsSend(row({ runnable: { status: 'ready', status_reason: null } }))).toBe(true);
		expect(acceptsSend(row({ runs_on: [{ ...other, selected: true }] }))).toBe(true);
	});

	it('takes a held send when only a usage limit blocks the model', () => {
		expect(acceptsSend(row({ runnable: blocked('usage_limited') }))).toBe(true);
		expect(acceptsSend(row())).toBe(true); // no runnable: the selected account is usage-limited
	});

	it('refuses every other block', () => {
		for (const reason of ['account_model_not_discovered', 'connection_unavailable', 'account_project_denied']) {
			expect(acceptsSend(row({ runnable: blocked(reason) }))).toBe(false);
		}
		expect(acceptsSend(row({ runs_on: undefined, status: 'unavailable', status_reason: 'connection_unavailable' }))).toBe(false);
	});
});
