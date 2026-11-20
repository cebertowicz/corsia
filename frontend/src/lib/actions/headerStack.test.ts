import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { fitsOneRow, headerStack, lineFloor, shouldStack } from './headerStack';

// What the review line needs in the first column: avatar, name at its floor, a 16-character ask, the chevron and the gaps between them.
const floor = 19 + 8 + 56 + 8 + 122 + 8 + 13;

describe('shouldStack', () => {
	const tight = { content: 636, others: 440, gaps: 8, floor };

	it('stacks a tight header on a tall viewport', () => {
		expect(shouldStack(tight, false)).toBe(true);
	});

	it('keeps the same tight header on one row on a short viewport', () => {
		expect(shouldStack(tight, true)).toBe(false);
	});

	it('still stacks a header narrower than 408px on a short viewport', () => {
		expect(shouldStack({ ...tight, content: 407 }, true)).toBe(true);
		expect(shouldStack({ ...tight, content: 408 }, true)).toBe(false);
	});
});

describe('lineFloor', () => {
	// jsdom draws nothing: give the pieces the widths the browser gave them (avatar 19, a 16ch probe 122, one gap 8, chevron 13, a short name 116 wide).
	afterEach(() => vi.restoreAllMocks());
	const draw = (html: string): HTMLElement => {
		document.body.innerHTML = `<div id="line">${html}</div>`;
		vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
			const el = this as HTMLElement;
			const width = el.style.width === '16ch' ? 122 : el.style.width === 'var(--space-2)' ? 8 : el.matches('.avatar') ? 19 : el.matches('.rb-chev') ? 13 : 0;
			return { width } as DOMRect;
		});
		const line = document.getElementById('line') as HTMLElement;
		Object.defineProperty(line.querySelector('.rb-who'), 'scrollWidth', { value: 116 });
		return line;
	};
	const lead = '<span class="rb-lead"><span class="avatar"></span><span class="rb-who">Rollout agent:</span></span>';

	it('holds no ask or chevron for a line that draws no request', () => {
		expect(lineFloor(draw(lead))).toBe(19 + 8 + 116);
	});

	it('gives a cut name the ask\'s 16 characters when no request is drawn, never its bare cut floor', () => {
		const cut = lead.replace('class="rb-who"', 'class="rb-who" style="min-width:70px"');
		expect(lineFloor(draw(cut))).toBe(19 + 8 + 122);
	});

	it('holds the 16-character ask and the chevron for a line that draws a request', () => {
		expect(lineFloor(draw(`${lead}<span class="rb-ask">Review</span><svg class="rb-chev"></svg>`))).toBe(19 + 8 + 116 + 8 + 122 + 8 + 13);
	});
});

describe('fitsOneRow', () => {
	it('holds the line beside the tools when the first column keeps its floors', () => {
		expect(fitsOneRow({ content: 900, others: 440, gaps: 8, floor })).toBe(true);
	});

	it('stacks when the tools as drawn leave the line less than its floors', () => {
		expect(fitsOneRow({ content: 636, others: 440, gaps: 8, floor })).toBe(false);
	});

	it('stacks a narrow phone header whose tools are small, with a name that is not cut', () => {
		const wholeNameFloor = 19 + 8 + 129 + 8 + 122 + 8 + 13;
		expect(fitsOneRow({ content: 345, others: 94, gaps: 8, floor: wholeNameFloor })).toBe(false);
	});

	it('fits exactly at the floor and not one pixel under', () => {
		expect(fitsOneRow({ content: floor + 440 + 8, others: 440, gaps: 8, floor })).toBe(true);
		expect(fitsOneRow({ content: floor + 440 + 8 - 1, others: 440, gaps: 8, floor })).toBe(false);
	});

	it('counts the presence row with the tools', () => {
		expect(fitsOneRow({ content: 700, others: 500, gaps: 8, floor })).toBe(false);
		expect(fitsOneRow({ content: 700, others: 440, gaps: 8, floor })).toBe(true);
	});
});

describe('one short-touch screen', () => {
	// A matchMedia that answers the real query for a screen of the given height and pointer (1em = 16px).
	const screen = (height: number, coarse: boolean) => {
		vi.stubGlobal('matchMedia', (query: string) => {
			const maxHeight = Number(/max-height: (\d+)em/.exec(query)?.[1]) * 16;
			const needsCoarse = query.includes('any-pointer: coarse');
			return { matches: height <= maxHeight && (!needsCoarse || coarse), addEventListener() {}, removeEventListener() {} };
		});
		vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	};
	// A header whose tools leave the notice less than its 300px floor: it stacks for room unless the screen is a short touch one.
	const stacked = (): boolean => {
		document.body.innerHTML = '<div id="h"><div class="rb-slot"></div><div class="document-tools"><span></span></div></div>';
		const node = document.getElementById('h') as HTMLElement;
		Object.defineProperty(node, 'clientWidth', { value: 636 });
		vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
			const el = this as HTMLElement;
			return { width: el.style.width === '16ch' ? 300 : el.parentElement?.matches('.document-tools') ? 440 : 0 } as DOMRect;
		});
		headerStack(node);
		return node.classList.contains('rb-stack');
	};
	const sheet = async (): Promise<boolean> => {
		vi.resetModules();
		const { isShortTouch } = await import('$lib/stores/viewport');
		return get(isShortTouch);
	};
	afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

	it('a 430px-tall touch screen gets the comments sheet and the short header', async () => {
		screen(430, true);
		expect(await sheet()).toBe(true);
		expect(stacked()).toBe(false);
	});

	it('a 420px-tall mouse window gets neither', async () => {
		screen(420, false);
		expect(await sheet()).toBe(false);
		expect(stacked()).toBe(true);
	});
});
