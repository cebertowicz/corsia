import { describe, expect, it } from 'vitest';
import type { Model } from '$lib/stores/models';
import { withAccountTurn } from '$lib/stores/modelFixtures';
import { chatPickerWarning } from './modelWarning';

function row(id: string, status: Model['status'], status_reason?: string): Model {
	return { id, name: id, size: '', backend: 'network', speed: '', status, status_reason };
}

describe('chatPickerWarning', () => {
	const catalog = [
		row('ext-ready', 'ready'),
		row('ext-nowin', 'unavailable', 'missing_context_window'),
		row('codex/x', 'unavailable', 'connection_unavailable'),
		row('cc/y', 'unavailable', 'discovered_model_unavailable'),
		row('vmlx-a', 'available'),
	];

	it('is silent for a model the picker lists', () => {
		expect(chatPickerWarning(catalog, 'ext-ready', true)).toBeNull();
	});

	it('is silent for a loadable oMLX model that is not loaded yet', () => {
		expect(chatPickerWarning(catalog, 'vmlx-a', false)).toBeNull();
	});

	it('is silent when the row is not in the catalog or has no id', () => {
		expect(chatPickerWarning(catalog, 'ext-unknown', true)).toBeNull();
		expect(chatPickerWarning(catalog, undefined, true)).toBeNull();
	});

	it("is silent when the only block is the selected account's usage limit", () => {
		const limited = withAccountTurn({ ...row('codex/z', 'unavailable', 'usage_limited'), status_resets_at: '2026-10-01T09:32:00+00:00' });
		expect(chatPickerWarning([limited], 'codex/z', false)).toBeNull();
		const noReset = withAccountTurn({ ...row('codex/z', 'unavailable', 'usage_limited'), usage: { state: 'limited', usable: false, windows: [] } });
		expect(chatPickerWarning([noReset], 'codex/z', false)).toBeNull();
	});

	it('points External rows at the checkpoint window setting', () => {
		expect(chatPickerWarning(catalog, 'ext-nowin', true)).toBe('Not shown in the model list: set its context window in this model’s details.');
	});

	it('does not point non-External rows at a setting they lack', () => {
		const text = chatPickerWarning(catalog, 'ext-nowin', false);
		expect(text).toContain('context window');
		expect(text).not.toContain('details');
	});

	it('names a disconnected harness', () => {
		expect(chatPickerWarning(catalog, 'codex/x', false)).toBe('Not shown in the model list: its app isn’t connected.');
	});

	it('falls back to a plain line for other reasons, never the raw code', () => {
		const text = chatPickerWarning(catalog, 'cc/y', false);
		expect(text).toBe('Not shown in the model list: this model is unavailable right now.');
		expect(text).not.toContain('discovered_model_unavailable');
	});
});
