import { afterEach, describe, expect, it, vi } from 'vitest';
import { capSheetBelowAnchor, clickedOpener, fitHalfSheet, onSheetMoved, openerFocus, presence, scrollDocumentForCard, scrollListToCard } from './docCommentDrawer';

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

describe('capSheetBelowAnchor', () => {
	const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
	function sheet(backdropBottom: number) {
		document.body.innerHTML = '<div data-adaptive-drawer><div class="panel"></div></div>';
		const backdrop = document.querySelector<HTMLElement>('[data-adaptive-drawer]')!;
		backdrop.getBoundingClientRect = () => ({ bottom: backdropBottom } as DOMRect);
		return document.querySelector<HTMLElement>('.panel')!;
	}

	it('leaves the room under the highlighted text, with a gap, and takes the cap off when stopped', async () => {
		const panel = sheet(852);
		const stop = capSheetBelowAnchor(panel, () => 189);
		await frame(); await frame();
		expect(panel.style.getPropertyValue('--drawer-cap')).toBe('647px');
		stop();
		expect(panel.style.getPropertyValue('--drawer-cap')).toBe('');
	});

	it('sets no cap when there is no anchor to keep in view, and never a negative one', async () => {
		const panel = sheet(300);
		const stop = capSheetBelowAnchor(panel, () => null);
		await frame(); await frame();
		expect(panel.style.getPropertyValue('--drawer-cap')).toBe('');
		stop();
		const low = capSheetBelowAnchor(panel, () => 900);
		await frame(); await frame();
		expect(panel.style.getPropertyValue('--drawer-cap')).toBe('0px');
		low();
	});

	it('does nothing without a sheet', () => {
		expect(() => capSheetBelowAnchor(null, () => 10)()).not.toThrow();
	});
});

describe('fitHalfSheet', () => {
	const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
	// A panel whose chrome is 80px and whose list holds 300px of card, under a backdrop of the given height.
	function sheet(backdropHeight: number) {
		document.body.innerHTML = '<div data-adaptive-drawer><div class="panel"><div class="drawer-comments"></div></div></div>';
		const backdrop = document.querySelector<HTMLElement>('[data-adaptive-drawer]')!;
		backdrop.getBoundingClientRect = () => ({ bottom: backdropHeight } as DOMRect);
		Object.defineProperty(backdrop, 'clientHeight', { value: backdropHeight });
		const panel = document.querySelector<HTMLElement>('.panel')!;
		const list = document.querySelector<HTMLElement>('.drawer-comments')!;
		Object.defineProperty(panel, 'offsetHeight', { value: 200 });
		Object.defineProperty(list, 'clientHeight', { value: 120 });
		Object.defineProperty(list, 'scrollHeight', { value: 300 });
		return panel;
	}

	it('keeps a line of text above the sheet on a tall screen', async () => {
		const panel = sheet(852);
		const stop = fitHalfSheet(panel, () => 600);
		await frame(); await frame();
		// Room is 852 - 600 - 120 = 132, under the 380px the whole card would take.
		expect(panel.style.getPropertyValue('--drawer-half-fit')).toBe('132px');
		stop();
		expect(panel.style.getPropertyValue('--drawer-half-fit')).toBe('');
	});

	it('keeps no text above the sheet on a short screen, so the card head and reply field are both in view first', async () => {
		const panel = sheet(393);
		const stop = fitHalfSheet(panel, () => 129);
		await frame(); await frame();
		// Room is 393 - 129 = 264, under the 380px the whole card would take; a tall screen would have left 144.
		expect(panel.style.getPropertyValue('--drawer-half-fit')).toBe('264px');
		stop();
	});

	it('does nothing without a sheet', () => {
		expect(() => fitHalfSheet(null, () => 10)()).not.toThrow();
	});
});

describe('scrollDocumentForCard', () => {
	function setup({ cardTop = 214, offsetHeight = 149, scrollHeight = 191, scrollTop = 0, maxScroll = 400, scrollerTop = 112, innerHeight = 393 } = {}) {
		document.body.innerHTML = '<div class="bounds"><div class="cm-scroller"></div></div><div class="card"></div>';
		const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
		const card = document.querySelector<HTMLElement>('.card')!;
		scroller.getBoundingClientRect = () => ({ top: scrollerTop } as DOMRect);
		Object.defineProperty(scroller, 'scrollHeight', { value: maxScroll + 255 });
		Object.defineProperty(scroller, 'clientHeight', { value: 255 });
		scroller.scrollTop = scrollTop;
		card.getBoundingClientRect = () => ({ top: cardTop } as DOMRect);
		Object.defineProperty(card, 'offsetHeight', { value: offsetHeight });
		Object.defineProperty(card, 'clientHeight', { value: offsetHeight - 2 });
		Object.defineProperty(card, 'scrollHeight', { value: scrollHeight });
		Object.defineProperty(window, 'innerHeight', { value: innerHeight, configurable: true });
		return { card, scroller, bounds: document.querySelector<HTMLElement>('.bounds')! };
	}
	const anchor = { top: 175 } as DOMRect;

	it('scrolls the document up by the card’s shortfall when the highlight can stay in view', () => {
		const { card, scroller, bounds } = setup();
		expect(scrollDocumentForCard(card, anchor, bounds)).toBe(44);
		expect(scroller.scrollTop).toBe(44);
	});

	it('stops where the highlight would leave the document’s top, and where the document ends', () => {
		const a = setup({ scrollerTop: 160 });
		expect(scrollDocumentForCard(a.card, anchor, a.bounds)).toBe(7);
		const b = setup({ scrollTop: 395 });
		expect(scrollDocumentForCard(b.card, anchor, b.bounds)).toBe(5);
	});

	it('does nothing when the card is whole, sits above its highlight, or has no document', () => {
		const whole = setup({ offsetHeight: 193 });
		expect(scrollDocumentForCard(whole.card, anchor, whole.bounds)).toBe(0);
		const above = setup({ cardTop: 100 });
		expect(scrollDocumentForCard(above.card, anchor, above.bounds)).toBe(0);
		expect(scrollDocumentForCard(above.card, anchor, null)).toBe(0);
		expect(scrollDocumentForCard(null, anchor, above.bounds)).toBe(0);
	});
});
