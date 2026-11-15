import { describe, expect, it } from 'vitest';
import { capSheetAboveText, giveBackRoom, keptWhole, placeAboveSheet, SHEET_GAP } from './sheetClearance';

function sheet(backdropBottom: number): HTMLElement {
	const backdrop = document.createElement('div');
	backdrop.setAttribute('data-adaptive-drawer', '');
	backdrop.getBoundingClientRect = () => ({ bottom: backdropBottom, top: 0, left: 0, right: 393, width: 393, height: backdropBottom, x: 0, y: 0, toJSON: () => ({}) });
	const panel = document.createElement('div');
	backdrop.append(panel);
	document.body.append(backdrop);
	return panel;
}

describe('capSheetAboveText', () => {
	it('lets the sheet rise only as far as leaves the text and a gap each side above it', () => {
		const panel = sheet(852);
		capSheetAboveText(panel, { height: 46, regionTop: 122 });
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe(`${852 - (122 + SHEET_GAP + 46 + SHEET_GAP)}px`);
	});

	it('never caps the sheet below a usable height', () => {
		const panel = sheet(568);
		capSheetAboveText(panel, { height: 400, regionTop: 100 });
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe('200px');
	});

	it('gives the same cap however often it runs', () => {
		const panel = sheet(852);
		capSheetAboveText(panel, { height: 21, regionTop: 125 });
		const first = panel.style.getPropertyValue('--drawer-text-cap');
		capSheetAboveText(panel, { height: 21, regionTop: 125 });
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe(first);
	});

	it('does nothing for a panel that is not in a drawer', () => {
		const panel = document.createElement('div');
		capSheetAboveText(panel, { height: 21, regionTop: 125 });
		expect(panel.style.getPropertyValue('--drawer-text-cap')).toBe('');
	});
});

function scroller(scrollHeight: number, clientHeight: number, scrollTop: number): HTMLElement {
	const el = document.createElement('div');
	Object.defineProperty(el, 'scrollHeight', { value: scrollHeight });
	Object.defineProperty(el, 'clientHeight', { value: clientHeight });
	el.scrollTop = scrollTop;
	el.style.paddingBottom = '10px';
	document.body.append(el);
	return el;
}

// The space for text: from the band's top plus a gap to the sheet's top less a gap (here 116 to 464, 348 tall).
const band = { top: 100, sheetTop: 480 };

describe('placeAboveSheet', () => {
	it('centres text the sheet covers in the space above it', () => {
		const el = scroller(2000, 600, 300);
		placeAboveSheet(el, { top: 500, bottom: 521 }, band, true);
		// Centre of the space is 290; the text's centre is 510.5.
		expect(el.scrollTop).toBe(300 + 510.5 - 290);
	});

	it('leaves text that is already in the space where it is', () => {
		const el = scroller(2000, 600, 300);
		placeAboveSheet(el, { top: 200, bottom: 221 }, band, true);
		expect(el.scrollTop).toBe(300);
	});

	it('keeps a gap between the text and the sheet: text touching the sheet moves', () => {
		const el = scroller(2000, 600, 300);
		placeAboveSheet(el, { top: 450, bottom: 480 - SHEET_GAP + 1 }, band, true);
		expect(el.scrollTop).toBeGreaterThan(300);
	});

	it('after opening, lifts only text a movement has put under the sheet, never text the reader scrolled above it', () => {
		const el = scroller(2000, 600, 300);
		placeAboveSheet(el, { top: 20, bottom: 41 }, band, false);
		expect(el.scrollTop).toBe(300);
		placeAboveSheet(el, { top: 500, bottom: 521 }, band, false);
		expect(el.scrollTop).toBeGreaterThan(300);
	});

	it('shows text taller than the space from its first line', () => {
		const el = scroller(2000, 600, 300);
		placeAboveSheet(el, { top: 500, bottom: 1000 }, band, true);
		expect(el.scrollTop).toBe(300 + 500 - (band.top + SHEET_GAP));
	});

	it('adds only the room missing below the content, on top of the padding it has, and gives it back', () => {
		// 20 left below; the lift is 510.5 - 290 = 220.5, so 201 more (rounded up) on top of 10.
		const el = scroller(1000, 600, 380);
		placeAboveSheet(el, { top: 500, bottom: 521 }, band, true);
		expect(el.style.paddingBottom).toBe('211px');
		giveBackRoom(el);
		expect(el.style.paddingBottom).toBe('10px');
	});

	it('adds nothing when the content already has the room, and giving back changes nothing', () => {
		const el = scroller(2000, 600, 300);
		placeAboveSheet(el, { top: 500, bottom: 521 }, band, true);
		giveBackRoom(el);
		expect(el.style.paddingBottom).toBe('10px');
	});
});

describe('keptWhole', () => {
	it('keeps a highlight of up to three lines whole and only the pressed line of a taller one', () => {
		expect(keptWhole(21, 21)).toBe(true);
		expect(keptWhole(63, 21)).toBe(true);
		expect(keptWhole(64, 21)).toBe(false);
	});
});
