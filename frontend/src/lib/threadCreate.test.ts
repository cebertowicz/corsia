import { describe, expect, it } from 'vitest';
import { buildNewThreadBody, prepareDraftThread } from './threadCreate';
import type { Model } from './stores/models';

describe('session creation helpers', () => {
	it('sends an explicit account and omits it when no account was chosen', () => {
		expect(buildNewThreadBody({ modelId: 'codex/model', accountId: 'chosen' })).toEqual({ title: 'New Chat', model: 'codex/model', account_id: 'chosen' });
		expect(buildNewThreadBody({ modelId: 'codex/model' })).toEqual({ title: 'New Chat', model: 'codex/model' });
	});
	it('sends the choice matching the current model vendor after a model change', () => {
		const models = [{ id: 'codex/model', connection: 'codex' }] as Model[];
		const result = prepareDraftThread('project-1', null, 'codex/model', models, { codex: 'chosen', 'claude-code': 'other' });
		expect(result.vendor).toBe('codex');
		expect(result.body).toEqual({ title: 'New Chat', project_id: 'project-1', model: 'codex/model', account_id: 'chosen' });
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
