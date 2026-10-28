import { describe, expect, it, vi } from 'vitest';
import { buildNewThreadBody, createAccountRefusal, draftAccountForModel } from './threadCreate';
import { CREATE_ACCOUNT_REFUSED } from './messageDeliveryCopy';
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

	it('keeps a refused choice and refreshes its status, clearing only a removed account', async () => {
		const clear = vi.fn();
		const reload = vi.fn(async () => {});
		expect(await createAccountRefusal(400, 'account_signed_out; retry', clear, reload)).toBe(true);
		expect(clear).not.toHaveBeenCalled();
		expect(reload).toHaveBeenCalledTimes(1);
		expect(await createAccountRefusal(400, 'account_removed; account does not exist for this provider', clear, reload)).toBe(true);
		expect(clear).toHaveBeenCalledTimes(1);
		expect(reload).toHaveBeenCalledTimes(2);
		expect(await createAccountRefusal(400, 'account does not exist for this provider', clear, reload)).toBe(false);
		expect(await createAccountRefusal(500, 'account_removed; retry', clear, reload)).toBe(false);
		expect(CREATE_ACCOUNT_REFUSED).toBe("Couldn't start the chat on the account you picked. Pick another account in the model menu and send again.");
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
