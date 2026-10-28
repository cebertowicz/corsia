import { get } from 'svelte/store';
import { afterEach, describe, expect, it } from 'vitest';
import { markDraftAccount, type PickerAccount } from './accountPicker';
import { choosePickerAccount } from './pickerAccountChoice';
import { composerSelectionFor, draftAccountChoices } from './composerSelection';
import { activeThreadId } from './threads';
import { activeModelId } from './modelsState';
import type { VendorAccount } from './vendorAccounts';

function row(provider: string, id: string, marked = false): PickerAccount {
	return { account: { id, provider, state: 'signed_in' } as VendorAccount, marked, pending: false, order: null, choosable: true };
}

afterEach(() => { activeThreadId.set(null); draftAccountChoices.set({}); activeModelId.set(null); });

describe('draft account choices', () => {
	it.each(['claude-code', 'codex'])('marks the selected %s row and keeps the model', async (vendor) => {
		activeThreadId.set(null);
		activeModelId.set('some-model');
		const rows = [row(vendor, 'first', true), row(vendor, 'second')];
		await choosePickerAccount(null, 'some-model', null, rows[1]);
		const selection = get(composerSelectionFor)(null);
		expect(markDraftAccount(rows, selection.accountChoices[vendor]).map((r) => r.marked)).toEqual([false, true]);
		expect(selection.modelId).toBe('some-model');
	});

	it('keeps one choice per vendor across a model change and clears on a thread', async () => {
		activeThreadId.set(null);
		await choosePickerAccount(null, 'claude-code/model', null, row('codex', 'c'));
		await choosePickerAccount(null, 'claude-code/model', null, row('claude-code', 'a'));
		activeModelId.set('codex/model');
		expect(get(draftAccountChoices)).toEqual({ codex: 'c', 'claude-code': 'a' });
		activeThreadId.set('created');
		expect(get(draftAccountChoices)).toEqual({});
	});
});
