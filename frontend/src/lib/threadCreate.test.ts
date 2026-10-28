import { describe, expect, it, vi } from 'vitest';
import { buildNewThreadBody, createFailedMessage, draftAccountForModel, reloadOnAccountRefusal } from './threadCreate';
import { CREATE_ACCOUNT_REFUSED } from './messageDeliveryCopy';
import type { Model } from './stores/models';

describe('session creation helpers', () => {
	it('sends an explicit account and omits it when no account was chosen', () => {
		expect(buildNewThreadBody({ modelId: 'codex/model', accountId: 'chosen' })).toEqual({ title: 'New Chat', model: 'codex/model', account_id: 'chosen' });
		expect(buildNewThreadBody({ modelId: 'codex/model' })).toEqual({ title: 'New Chat', model: 'codex/model' });
	});
	it('sends any choice of the current model vendor, so creation admits or refuses it before the first send', () => {
		const models = [{ id: 'codex/model', runs_on: [{ account_id: 'chosen', selected: true }, { account_id: 'other', selected: false }] }] as Model[];
		expect(draftAccountForModel('chosen', 'codex/model', models)).toBe('chosen');
		expect(draftAccountForModel('other', 'codex/model', models)).toBe('other');
		expect(draftAccountForModel('claude-account', 'codex/model', models)).toBeNull();
	});

	it('refreshes every coded account refusal without clearing the choice', async () => {
		const reload = vi.fn(async () => {});
		expect(await reloadOnAccountRefusal(400, 'account_signed_out; retry', reload)).toBe(true);
		expect(await reloadOnAccountRefusal(400, 'account_removed; choose a permitted account or restore this account\'s eligibility', reload)).toBe(true);
		expect(await reloadOnAccountRefusal(400, 'account_actor_unauthorized', reload)).toBe(true);
		expect(reload).toHaveBeenCalledTimes(3);
		expect(await reloadOnAccountRefusal(400, 'account does not exist for this provider', reload)).toBe(false);
		expect(await reloadOnAccountRefusal(500, 'account_removed; retry', reload)).toBe(false);
		expect(CREATE_ACCOUNT_REFUSED).toBe('Couldn’t start the chat on that account. Its row in the model picker says why; pick another account there.');
	});

	it('shows the server reference beside the generic create failure', () => {
		expect(createFailedMessage('err-0123abcd')).toBe('Couldn’t create the chat. Try again. Reference: err-0123abcd');
		expect(createFailedMessage(undefined)).toBe('Couldn’t create the chat. Try again.');
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
