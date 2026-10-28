import { describe, expect, it, vi } from 'vitest';
import { buildNewThreadBody, createAccountRefusal, draftAccountForModel } from './threadCreate';
import { PIN_FAILED, TURN_RUNNING } from './stores/threadAccountPin';
import type { Model } from './stores/models';

describe('session creation helpers', () => {
	it('sends an explicit account and omits it when no account was chosen', () => {
		expect(buildNewThreadBody({ modelId: 'codex/model', accountId: 'chosen' })).toEqual({ title: 'New Chat', model: 'codex/model', account_id: 'chosen' });
		expect(buildNewThreadBody({ modelId: 'codex/model' })).toEqual({ title: 'New Chat', model: 'codex/model' });
	});
	it('sends the choice matching the current model vendor after a model change', () => {
		const models = [{ id: 'codex/model', runs_on: [{ account_id: 'chosen', selected: true }, { account_id: 'other', selected: false }] }] as Model[];
		expect(draftAccountForModel('chosen', 'codex/model', models)).toBe('chosen');
		expect(draftAccountForModel('other', 'codex/model', models)).toBeNull();
	});

	it('maps account refusals using existing pin copy', () => {
		const clear = vi.fn();
		expect(createAccountRefusal(400, 'account_change_requires_idle; retry', clear)).toBe(TURN_RUNNING);
		expect(createAccountRefusal(400, 'account_signed_out; retry', clear)).toBe('signed out');
		expect(createAccountRefusal(400, 'usage_limited; retry', clear)).toBe('limited');
		expect(createAccountRefusal(400, 'account_project_denied; retry', clear)).toBe('Your provider settings don’t allow this chat’s account.');
		expect(createAccountRefusal(400, 'account_removed; retry', clear)).toBe(PIN_FAILED);
		expect(createAccountRefusal(400, 'account does not exist for this provider', clear)).toBe(PIN_FAILED);
		expect(clear).toHaveBeenCalledTimes(6);
		expect(createAccountRefusal(500, 'account_removed; retry', clear)).toBeNull();
		expect(clear).toHaveBeenCalledTimes(6);
	});

	it('builds the create-session payload from selected project and model state', () => {
		expect(buildNewThreadBody({
			projectId: 'project-1',
			reasoningEffort: 'high',
			modelId: 'omlx@local/qwen',
		})).toEqual({
			title: 'New Chat',
			project_id: 'project-1',
			model: 'omlx@local/qwen',
			reasoning_effort: 'high',
		});
	});
});
