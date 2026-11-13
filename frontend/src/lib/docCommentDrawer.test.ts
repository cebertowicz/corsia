import { afterEach, describe, expect, it, vi } from 'vitest';
import { clickedOpener, onSheetMoved, openerFocus, presence, scrollListToCard } from './docCommentDrawer';

afterEach(() => { document.body.replaceChildren(); vi.unstubAllGlobals(); });

it('keeps a visible candidate in place while still scrolling ordinary or offscreen cards', () => {
	document.body.innerHTML = '<section><div class="drawer-comments"><div><div data-comment-id="a"></div></div></div></section>';
	const drawer = document.querySelector<HTMLElement>('section')!;
	const list = drawer.querySelector<HTMLElement>('.drawer-comments')!;
	const card = drawer.querySelector<HTMLElement>('[data-comment-id]')!;
	list.getBoundingClientRect = () => ({ top: 100, bottom: 500 } as DOMRect);
	card.getBoundingClientRect = () => ({ top: 200, bottom: 300 } as DOMRect);
	list.scrollTo = vi.fn();
	scrollListToCard(drawer, 'a', false, true);
	expect(list.scrollTo).not.toHaveBeenCalled();
	scrollListToCard(drawer, 'a', true, true);
	expect(list.scrollTo).toHaveBeenCalledTimes(1);
	card.getBoundingClientRect = () => ({ top: 600, bottom: 700 } as DOMRect);
	scrollListToCard(drawer, 'a', false, true);
	expect(list.scrollTo).toHaveBeenCalledTimes(2);
	card.getBoundingClientRect = () => ({ top: 200, bottom: 300 } as DOMRect);
	scrollListToCard(drawer, 'a', false);
	expect(list.scrollTo).toHaveBeenCalledTimes(3);
});

describe('openerFocus', () => {
	function list() {
		document.body.innerHTML = '<div data-comment-id="c1"><div class="ct"><button type="button" class="ct-num">1</button><p>text</p><button type="button">Resolve</button></div></div><button type="button" data-region-thumb>thumb</button>';
		return {
			card: document.querySelector<HTMLElement>('[data-comment-id]')!,
			number: document.querySelector<HTMLElement>('.ct-num')!,
			resolve: document.querySelectorAll<HTMLElement>('button')[1],
			thumb: document.querySelector<HTMLElement>('[data-region-thumb]')!,
		};
	}

	it('finds the card or thumbnail a click landed on', () => {
		const { card, thumb } = list();
		const click = (target: Element) => { const event = new MouseEvent('click', { bubbles: true }); target.dispatchEvent(event); return event; };
		let seen: Element | null = null;
		document.addEventListener('click', (event) => { seen = clickedOpener(event); }, { once: true });
		click(card.querySelector('p')!);
		expect(seen).toBe(card);
		document.addEventListener('click', (event) => { seen = clickedOpener(event); }, { once: true });
		click(thumb);
		expect(seen).toBe(thumb);
		document.addEventListener('click', (event) => { seen = clickedOpener(event); }, { once: true });
		click(document.body);
		expect(seen).toBeNull();
	});

	it('returns to what had focus inside the opener, else its own first control', () => {
		const { card, number, resolve, thumb } = list();
		expect(openerFocus(card, resolve)).toBe(resolve);
		// A click on a card's text focuses nothing: its number, which shows it on the image, takes focus back.
		expect(openerFocus(card, document.body)).toBe(number);
		expect(openerFocus(thumb, thumb)).toBe(thumb);
		expect(openerFocus(thumb, document.body)).toBe(thumb);
	});

	it('has nowhere to return to once the opener has gone', () => {
		const { card } = list();
		card.remove();
		expect(openerFocus(card, null)).toBeNull();
		expect(openerFocus(null, null)).toBeNull();
	});
});

describe('presence', () => {
	it('reports shown when the first element mounts and gone only once the last has left', () => {
		const reports: boolean[] = [];
		const shown = presence((value) => reports.push(value));
		const sheet = shown(document.createElement('section'));
		expect(reports).toEqual([true]);
		// The drawer changes form: the new one mounts before the old one's outro ends, and nothing is reported.
		const beside = shown(document.createElement('section'));
		sheet.destroy();
		expect(reports).toEqual([true]);
		beside.destroy();
		expect(reports).toEqual([true, false]);
	});
});

describe('onSheetMoved', () => {
	it('asks to open once the sheet is up, then to move when it resizes or the visual viewport moves, one frame later', async () => {
		const viewport = new EventTarget();
		vi.stubGlobal('visualViewport', viewport);
		const panel = document.createElement('div'), reveal = vi.fn();
		const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
		const stop = onSheetMoved(panel, reveal);
		await frame(); await frame();
		expect(reveal.mock.calls).toEqual([[true]]);
		viewport.dispatchEvent(new Event('resize'));
		await frame(); await frame();
		viewport.dispatchEvent(new Event('scroll'));
		await frame(); await frame();
		expect(reveal.mock.calls).toEqual([[true], [false], [false]]);
		stop();
		viewport.dispatchEvent(new Event('resize'));
		await frame(); await frame();
		expect(reveal).toHaveBeenCalledTimes(3);
	});

	it('watches nothing without a sheet or something to call', async () => {
		const reveal = vi.fn();
		expect(() => onSheetMoved(null, reveal)()).not.toThrow();
		expect(() => onSheetMoved(document.createElement('div'))()).not.toThrow();
		await new Promise((resolve) => requestAnimationFrame(resolve));
		expect(reveal).not.toHaveBeenCalled();
	});
});
