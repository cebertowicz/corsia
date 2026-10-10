import { describe, expect, it } from 'vitest';
import { pickerModels, type Model } from './models';

function row(id: string, status: Model['status'], alias_of?: string): Model {
	return { id, name: id, size: '', backend: 'cc', speed: '', status, alias_of, litellm_model: id };
}

describe('pickerModels', () => {
	const catalog = [
		row('claude-code/opus', 'ready'),
		row('claude-code/opus[1m]', 'ready', 'claude-code/opus'),
		row('claude-code/haiku', 'unavailable'),
	];

	it('offers each model once: ready rows, without aliases', () => {
		expect(pickerModels(catalog).map((m) => m.id)).toEqual(['claude-code/opus']);
	});

	it('keeps an alias that is the current choice so it still shows as selected', () => {
		expect(pickerModels(catalog, 'claude-code/opus[1m]').map((m) => m.id)).toEqual([
			'claude-code/opus',
			'claude-code/opus[1m]',
		]);
	});

	it('never offers an unavailable row, even as the current choice', () => {
		expect(pickerModels(catalog, 'claude-code/haiku').map((m) => m.id)).toEqual(['claude-code/opus']);
	});
});
