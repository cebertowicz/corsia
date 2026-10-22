import { describe, expect, it } from 'vitest';
import type { RunsOn } from './stores/modelTurn';

import {
	bestSetupModel,
	setupModelWorks,
	type SetupModel,
} from './setupFirstModel';

function model(overrides: Partial<SetupModel> = {}): SetupModel {
	return {
		id: 'model',
		name: 'Model',
		size: '',
		backend: 'harness',
		speed: '',
		litellm_model: 'provider/model',
		status: 'ready',
		...overrides,
	} as SetupModel;
}

function account(overrides: Partial<RunsOn> = {}): RunsOn {
	return {
		account_id: 'setup-account', selected: true, status: 'ready',
		status_reason: null, resets_at: null,
		usage: { state: 'allowed', usable: true, windows: [] },
		...overrides,
	};
}

describe('setup first model', () => {
	it('requires a ready selected account for Claude Code and Codex', () => {
		const sourceOnly = model({
			connection: 'claude-code',
			runs_on: [account({ selected: false })],
		});
		const selectedUnavailable = model({
			connection: 'codex',
			runs_on: [account({ status: 'unavailable', status_reason: 'usage_limited' })],
		});
		const selectedReady = model({
			connection: 'claude-code',
			runs_on: [account()],
		});

		expect(setupModelWorks(sourceOnly)).toBe(false);
		expect(setupModelWorks(selectedUnavailable)).toBe(false);
		expect(setupModelWorks(selectedReady)).toBe(true);
	});

	it('uses Claude Code’s catalog default before Codex, API, and local rows', () => {
		const claude = model({
			id: 'claude-default', connection: 'claude-code', is_default: true,
			runs_on: [account()],
		});
		const codex = model({
			id: 'codex-default', connection: 'codex', is_default: true,
			runs_on: [account()],
		});
		const api = model({ id: 'api-default', provider: 'openai', is_default: true });
		const local = model({ id: 'local', connection: 'omlx' });

		expect(bestSetupModel([local, api, codex, claude], [{ source: 'openai' }]))
			.toBe(claude);
	});

	it('uses the first ready Claude row when its vendor reports no default', () => {
		const first = model({
			id: 'first-claude', connection: 'claude-code',
			runs_on: [account()],
		});
		const second = model({
			id: 'second-claude', connection: 'claude-code',
			runs_on: [account()],
		});

		expect(bestSetupModel([first, second])).toBe(first);
	});

	it('uses a tested API provider only when subscriptions are unavailable', () => {
		const api = model({ id: 'api', provider: 'anthropic', is_default: true });
		const local = model({ id: 'local', connection: 'omlx' });

		expect(bestSetupModel([local, api], [{ source: 'anthropic' }])).toBe(api);
	});
});
