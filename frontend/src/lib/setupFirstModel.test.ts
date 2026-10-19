import { describe, expect, it } from 'vitest';

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

describe('setup first model', () => {
	it('requires a ready selected account for Claude Code and Codex', () => {
		const sourceOnly = model({
			connection: 'claude-code',
			runs_on: [{ selected: false, status: 'ready' }],
		});
		const selectedUnavailable = model({
			connection: 'codex',
			runs_on: [{ selected: true, status: 'usage_limited' }],
		});
		const selectedReady = model({
			connection: 'claude-code',
			runs_on: [{ selected: true, status: 'ready' }],
		});

		expect(setupModelWorks(sourceOnly)).toBe(false);
		expect(setupModelWorks(selectedUnavailable)).toBe(false);
		expect(setupModelWorks(selectedReady)).toBe(true);
	});

	it('uses Claude Code’s catalog default before Codex, API, and local rows', () => {
		const claude = model({
			id: 'claude-default', connection: 'claude-code', is_default: true,
			runs_on: [{ selected: true, status: 'ready' }],
		});
		const codex = model({
			id: 'codex-default', connection: 'codex', is_default: true,
			runs_on: [{ selected: true, status: 'ready' }],
		});
		const api = model({ id: 'api-default', provider: 'openai', is_default: true });
		const local = model({ id: 'local', connection: 'omlx' });

		expect(bestSetupModel([local, api, codex, claude], [{ source: 'openai' }]))
			.toBe(claude);
	});

	it('uses the first ready Claude row when its vendor reports no default', () => {
		const first = model({
			id: 'first-claude', connection: 'claude-code',
			runs_on: [{ selected: true, status: 'ready' }],
		});
		const second = model({
			id: 'second-claude', connection: 'claude-code',
			runs_on: [{ selected: true, status: 'ready' }],
		});

		expect(bestSetupModel([first, second])).toBe(first);
	});

	it('uses a tested API provider only when subscriptions are unavailable', () => {
		const api = model({ id: 'api', provider: 'anthropic', is_default: true });
		const local = model({ id: 'local', connection: 'omlx' });

		expect(bestSetupModel([local, api], [{ source: 'anthropic' }])).toBe(api);
	});
});
