import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { clearDocPickerTarget, docPickerTarget } from './docPickerTarget';

describe('doc picker target', () => {
	beforeEach(() => docPickerTarget.set(null));

	it('clears the originating thread when navigation abandons the picker', () => {
		docPickerTarget.set({ threadId: 'thread-a' });
		clearDocPickerTarget();

		expect(get(docPickerTarget)).toBeNull();
	});
});
