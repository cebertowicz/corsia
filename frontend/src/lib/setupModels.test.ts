import { describe, expect, it } from 'vitest';
import type { RunsOn } from './stores/modelTurn';

import {
	hasReadySetupModel,
	setupModelWorks,
	type SetupModel,
} from './setupModels';

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

describe('setup model readiness', () => {
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

	it('counts a ready subscription, local model or tested API provider', () => {
		const claude = model({ connection: 'claude-code', runs_on: [account()] });
		const local = model({ connection: 'omlx' });
		const api = model({ provider: 'anthropic' });

		expect(hasReadySetupModel([claude])).toBe(true);
		expect(hasReadySetupModel([local])).toBe(true);
		expect(hasReadySetupModel([api], [{ source: 'anthropic' }])).toBe(true);
	});

	it('does not count an untested API provider or a subscription without a ready account', () => {
		const api = model({ provider: 'anthropic' });
		const claude = model({ connection: 'claude-code', runs_on: [account({ selected: false })] });

		expect(hasReadySetupModel([api])).toBe(false);
		expect(hasReadySetupModel([claude])).toBe(false);
		expect(hasReadySetupModel([])).toBe(false);
	});
});
