import { describe, expect, it } from 'vitest';
import { presenceMaximum } from './docPresenceFit';

describe('document header presence fit', () => {
	it('collapses three readers before cutting a long document name', () => {
		expect(presenceMaximum(210, 90, 55, 150, 55, false)).toBe(1);
	});

	it('keeps all readers when a short name fits', () => {
		expect(presenceMaximum(210, 90, 55, 80, 55, false)).toBe(3);
	});

	it('uses the count alone when the avatar would overlap it', () => {
		expect(presenceMaximum(150, 90, 55, 150, 55, true)).toBe(0);
	});

	it('uses the count alone when one avatar leaves less than the name floor', () => {
		expect(presenceMaximum(105, 90, 55, 150, 55, false)).toBe(0);
	});
});
