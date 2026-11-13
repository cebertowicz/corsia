import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { keepInView } from './keepInView';

// A sheet's scroller whose visible height can shrink while its content stays put, a card at the end of that content,
// and the card's reply field. Geometry is in content pixels; the scroller's top sits at a fixed place on screen.
const SCROLLER_TOP = 569;
const CONTENT = 497;
const CARD = { top: 306, bottom: 497 };
const FIELD_BOTTOM = 490;

let height = 497;
let scrollTop = 0;
const observers = new Map<Element, () => void>();

beforeEach(() => {
	height = 497;
	scrollTop = 0;
	observers.clear();
	vi.stubGlobal('ResizeObserver', class {
		constructor(private callback: () => void) {}
		observe(target: Element) { observers.set(target, this.callback); }
		disconnect() {}
	});
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
});
afterEach(() => {
	vi.unstubAllGlobals();
	document.body.replaceChildren();
});

const onScreen = (contentY: number) => SCROLLER_TOP + contentY - scrollTop;

function sheet() {
	const owner = document.createElement('div');
	owner.style.overflowY = 'auto';
	Object.defineProperty(owner, 'clientHeight', { get: () => height });
	Object.defineProperty(owner, 'scrollTop', { get: () => scrollTop, set: (value: number) => { scrollTop = value; } });
	owner.getBoundingClientRect = () => ({ top: SCROLLER_TOP, bottom: SCROLLER_TOP + height }) as DOMRect;
	const card = document.createElement('div');
	card.getBoundingClientRect = () => ({ top: onScreen(CARD.top), bottom: onScreen(CARD.bottom), height: CARD.bottom - CARD.top }) as DOMRect;
	// The browser's nearest-edge scroll: a card below the scroller's end lands with its bottom on that end.
	const reveals = vi.fn(() => { scrollTop = Math.max(0, Math.min(CONTENT - height, CARD.bottom - height)); });
	card.scrollIntoView = reveals;
	const field = document.createElement('textarea');
	field.getBoundingClientRect = () => ({ top: onScreen(FIELD_BOTTOM - 40), bottom: onScreen(FIELD_BOTTOM), height: 40 }) as DOMRect;
	card.append(field);
	owner.append(card);
	document.body.append(owner);
	const shrink = (to: number) => { height = to; observers.get(owner)?.(); };
	return { owner, card, field, reveals, shrink };
}

describe('keepInView', () => {
	it('keeps a picked card in view while its sheet shrinks, though its own scroll is read only after a further shrink', () => {
		const { owner, card, field, reveals, shrink } = sheet();
		const action = keepInView(field, { enabled: false, whole: card });
		action.update({ enabled: true, whole: card });
		expect(reveals).toHaveBeenCalledTimes(1);
		shrink(290);
		expect(reveals).toHaveBeenCalledTimes(2);
		// The sheet shrinks again before the scroll event of that reveal is dispatched, so the event reads the field
		// below the end. That scroll was this action's own, not the person's.
		height = 267;
		owner.dispatchEvent(new Event('scroll'));
		shrink(267);
		expect(reveals).toHaveBeenCalledTimes(3);
		expect(field.getBoundingClientRect().bottom).toBeLessThanOrEqual(owner.getBoundingClientRect().bottom);
		action.destroy();
	});

	it('leaves a person who scrolled away from the field where they are when the sheet resizes', () => {
		height = 290;
		const { owner, card, field, reveals, shrink } = sheet();
		const action = keepInView(field, { enabled: false, whole: card });
		action.update({ enabled: true, whole: card });
		expect(reveals).toHaveBeenCalledTimes(1);
		scrollTop = 0;
		owner.dispatchEvent(new Event('scroll'));
		shrink(290);
		expect(reveals).toHaveBeenCalledTimes(1);
		action.destroy();
	});

	it('brings the field back when the person types at its end, and not when they type inside it', () => {
		height = 290;
		const { owner, card, field, reveals } = sheet();
		const action = keepInView(field, { enabled: false, whole: card });
		action.update({ enabled: true, whole: card });
		expect(reveals).toHaveBeenCalledTimes(1);
		scrollTop = 0;
		owner.dispatchEvent(new Event('scroll'));
		field.value = 'a reply in progress';
		field.setSelectionRange(2, 2);
		field.dispatchEvent(new Event('input'));
		expect(reveals).toHaveBeenCalledTimes(1);
		field.setSelectionRange(field.value.length, field.value.length);
		field.dispatchEvent(new Event('input'));
		expect(reveals).toHaveBeenCalledTimes(2);
		action.destroy();
	});
});

/** A scroller holding a field and, below it, a sticky row pinned to the scroller's bottom. */
function pinnedScroller(pinnedHeight: number | null) {
	const owner = document.createElement('div');
	owner.style.overflowY = 'auto';
	const field = document.createElement('textarea');
	owner.append(field);
	if (pinnedHeight !== null) {
		const pin = document.createElement('div');
		pin.setAttribute('data-pin-bottom', '');
		pin.style.position = 'sticky';
		Object.defineProperty(pin, 'offsetHeight', { value: pinnedHeight });
		owner.append(pin);
	}
	document.body.append(owner);
	return { owner, field };
}


describe('keepInView with a pinned bottom row', () => {
	it('leaves the height of a sticky [data-pin-bottom] row clear at the scroller\'s bottom, for the browser\'s caret reveal too', () => {
		const { owner, field } = pinnedScroller(52);
		const action = keepInView(field, { enabled: true });
		action.update({ enabled: true });
		expect(owner.style.scrollPaddingBottom).toBe('52px');
		action.destroy();
		expect(owner.style.scrollPaddingBottom).toBe('');
	});

	it('adds no scroll padding when nothing is pinned', () => {
		const { owner, field } = pinnedScroller(null);
		const action = keepInView(field, { enabled: true });
		action.update({ enabled: true });
		expect(owner.style.scrollPaddingBottom).toBe('');
		action.destroy();
	});
	it('scrolls the scroller by what is left when a reveal still ends the field under the pinned row', () => {
		const { owner, field } = pinnedScroller(52);
		let top = 0;
		Object.defineProperty(owner, 'scrollTop', { get: () => top, set: (value: number) => { top = value; } });
		owner.getBoundingClientRect = () => ({ top: 0, bottom: 420, height: 420 }) as DOMRect;
		// scrollIntoView left the field's end at 400, 32 px under the row that covers 368 to 420.
		field.getBoundingClientRect = () => ({ top: 100, bottom: 400 - top, height: 300 }) as DOMRect;
		field.scrollIntoView = vi.fn();
		const action = keepInView(field, { enabled: true });
		action.update({ enabled: true });
		expect(top).toBe(32);
		action.destroy();
	});
});
