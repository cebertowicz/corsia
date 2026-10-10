import { get } from 'svelte/store';
import { afterEach, describe, expect, it } from 'vitest';

import { activeModelId, models, type Model } from './models';
import { limitHeldThreadId } from './overageSetting';
import { activeThreadId } from './threads';

const limitedAccount = {
	account_id: 'a', selected: true, status: 'unavailable' as const, status_reason: 'usage_limited', resets_at: '2099-01-01T00:00:00Z',
	usage: { state: 'limited' as const, usable: false, windows: [] },
};

function row(runnable: Model['runnable']): Model {
	return {
		id: 'cc/opus', name: 'Opus', size: '', backend: 'cc', speed: '', status: 'ready', connection: 'claude-code',
		litellm_model: 'claude-code/opus', runs_on: [limitedAccount], runnable,
	};
}

afterEach(() => {
	models.set([]);
	activeModelId.set(null);
	activeThreadId.set(null);
});

describe('limitHeldThreadId', () => {
	it('still holds a thread whose bound account is limited when another account could run the model', () => {
		models.set([row({ status: 'ready', status_reason: null })]);
		activeModelId.set('claude-code/opus');
		activeThreadId.set('thread-1');
		expect(get(limitHeldThreadId)).toBe('thread-1');
	});

	it('holds nothing while the bound account can run', () => {
		models.set([{ ...row({ status: 'ready', status_reason: null }), runs_on: [{ ...limitedAccount, status: 'ready', status_reason: null }] }]);
		activeModelId.set('claude-code/opus');
		activeThreadId.set('thread-1');
		expect(get(limitHeldThreadId)).toBeNull();
	});
});
