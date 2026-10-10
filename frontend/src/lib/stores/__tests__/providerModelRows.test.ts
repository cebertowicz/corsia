import { describe, expect, it } from 'vitest';
import { networkProviderModels } from '../providerModelRows';

describe('networkProviderModels', () => {
	it('shows the oMLX-reported context and the saved checkpoint cap', () => {
		const [model] = networkProviderModels({
			loaded: [{
				model_id: 'Qwen3.8-Flash-Next',
				display_name: 'Qwen3.8 Flash Next',
				context_length: 262_144,
				provider_context_length: 1_000_000,
				context_window_override: 262_144,
				db_id: 'omlx@srv-test/Qwen3.8-Flash-Next',
			}],
			available: [],
		});

		expect(model).toMatchObject({
			meta: '1M context · checkpoints at 262k',
			contextLength: 262_144,
			providerContextLength: 1_000_000,
		});
	});

	it('shows the CORSIA default checkpoint when nothing is saved', () => {
		const [model] = networkProviderModels({
			loaded: [{
				model_id: 'Qwen3.8-Flash-Next',
				context_length: 262_144,
				provider_context_length: 1_000_000,
				context_window_override: null,
				context_window_source: 'default',
			}],
			available: [],
		});

		expect(model).toMatchObject({ meta: '1M context · checkpoints at 262k' });
	});
});
