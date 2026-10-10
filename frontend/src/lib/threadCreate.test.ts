import { describe, expect, it } from 'vitest';
import { buildNewThreadBody } from './threadCreate';

describe('session creation helpers', () => {
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
