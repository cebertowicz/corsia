import { describe, expect, it } from 'vitest';
import { selectionButtonPlace } from './selectionButtonPlace';

describe('selectionButtonPlace', () => {
	it('sits above the selected text with a fine pointer when there is room', () => {
		expect(selectionButtonPlace({ top: 300, bottom: 360 }, false)).toEqual({ top: 294, above: true });
	});

	it('sits below the selected text on a touch screen', () => {
		expect(selectionButtonPlace({ top: 300, bottom: 360 }, true)).toEqual({ top: 366, above: false });
	});

	it('sits below the selected text when the text is near the window top', () => {
		expect(selectionButtonPlace({ top: 80, bottom: 120 }, false)).toEqual({ top: 126, above: false });
	});
});
