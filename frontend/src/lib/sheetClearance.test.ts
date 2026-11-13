import { describe, expect, it } from 'vitest';
import { capSheetAboveText, SHEET_GAP } from './sheetClearance';

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
