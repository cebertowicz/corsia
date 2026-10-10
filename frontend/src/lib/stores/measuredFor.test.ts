import { describe, expect, it } from 'vitest';

import { measuredFor } from './contextUsageByThread';
import type { Model } from './models';

function row(id: string, litellm_model?: string): Model {
	return { id, name: id, litellm_model } as Model;
}

function receipt(model: string) {
	return { thread_id: 't', receipt_model: model, total_used: 10, token_usage_available: true } as never;
}

describe('measuredFor model identity', () => {
	it('matches a dispatch id that is not a suffix of the selected id when the catalog row is the same', () => {
		const catalog = [row('local-fortissimo', 'omlx/qwen-flash-oq6')];
		expect(measuredFor(receipt('omlx/qwen-flash-oq6'), 't', 'local-fortissimo', catalog)).not.toBeNull();
	});

	it('does not match two different models that only share a suffix', () => {
		const catalog = [row('b/gpt', 'b/gpt'), row('gpt', 'gpt')];
		// The suffix rule matched the bare id "gpt" against "b/gpt"; they are two rows.
		expect(measuredFor(receipt('gpt'), 't', 'b/gpt', catalog)).toBeNull();
	});

	it('keeps the reading only on an exact string match when the catalog has no row', () => {
		expect(measuredFor(receipt('x'), 't', 'x', [])).not.toBeNull();
		expect(measuredFor(receipt('p/x'), 't', 'x', [])).toBeNull();
	});
});
