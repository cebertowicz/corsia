import { afterEach, describe, expect, it } from 'vitest';
import { clickedOpener, footerFocusHome, openerFocus } from './docCommentDrawer';

afterEach(() => document.body.replaceChildren());

/** A drawer with a heading to rest on and a footer holding Send now. */
function drawer() {
	document.body.innerHTML = '<section><h2 tabindex="-1">Comments</h2><div data-footer><button type="button">Send now</button></div></section><button type="button" data-outside>Elsewhere</button>';
	const heading = document.querySelector<HTMLElement>('h2')!;
	const footer = document.querySelector<HTMLElement>('[data-footer]')!;
	const send = footer.querySelector<HTMLButtonElement>('button')!;
	const action = footerFocusHome(footer, () => heading);
	return { heading, footer, send, action };
}
const settled = () => new Promise<void>((resolve) => queueMicrotask(resolve));

describe('footerFocusHome', () => {
	it('moves focus to the drawer heading when Send now is disabled under it and focus drops to the page', async () => {
		const { heading, send } = drawer();
		send.focus();
		// What a browser does to a focused control that is disabled, which jsdom does not: focus goes to the page, and
		// the control, disabled by then, reports focusout with nowhere it went.
		send.blur();
		send.disabled = true;
		send.dispatchEvent(new FocusEvent('focusout', { bubbles: true, relatedTarget: null }));
		await settled();
		expect(document.activeElement).toBe(heading);
	});

	it('moves focus to the drawer heading when the footer leaves while focus is inside it', async () => {
		const { heading, footer, send, action } = drawer();
		send.focus();
		footer.remove();
		action.destroy();
		await settled();
		expect(document.activeElement).toBe(heading);
	});

	it('leaves focus alone when it moves somewhere on purpose or the footer never held it', async () => {
		const { heading, footer, send, action } = drawer();
		const outside = document.querySelector<HTMLElement>('[data-outside]')!;
		send.focus();
		outside.focus();
		await settled();
		expect(document.activeElement).toBe(outside);
		// An enabled control blurred (the window losing focus, a click on bare text) is not the footer's doing.
		send.focus();
		send.blur();
		await settled();
		expect(document.activeElement).toBe(document.body);
		footer.remove();
		action.destroy();
		await settled();
		expect(document.activeElement).not.toBe(heading);
	});

	it('does nothing when the heading has gone with the drawer', async () => {
		const { heading, footer, send, action } = drawer();
		send.focus();
		heading.remove();
		footer.remove();
		action.destroy();
		await settled();
		expect(document.activeElement).toBe(document.body);
	});
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
