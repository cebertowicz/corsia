import { describe, expect, it } from 'vitest';
import { keepInBand, selectionButtonPlace } from './selectionButtonPlace';

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

describe('keepInBand', () => {
	const band = { top: 64, bottom: 872 };

	it('leaves a button that is inside the band where the selection puts it', () => {
		expect(keepInBand({ top: 294, above: true }, band, 28, 8)).toBe(294);
		expect(keepInBand({ top: 366, above: false }, band, 28, 8)).toBe(366);
	});

	it('holds a button below a selection taller than the window at the bottom of the band', () => {
		expect(keepInBand({ top: 926, above: false }, band, 28, 8)).toBe(872 - 8 - 28);
	});

	it('holds the button of a selection scrolled out of the top at the top of the band', () => {
		expect(keepInBand({ top: -400, above: false }, band, 28, 8)).toBe(64 + 8);
		expect(keepInBand({ top: -400, above: true }, band, 28, 8)).toBe(64 + 8 + 28);
	});

	it('keeps a button that hangs up from its top inside the band', () => {
		expect(keepInBand({ top: 1200, above: true }, band, 28, 8)).toBe(872 - 8);
	});

	it('uses the top of a band too short for the button', () => {
		expect(keepInBand({ top: 500, above: false }, { top: 100, bottom: 120 }, 28, 8)).toBe(108);
	});
});
