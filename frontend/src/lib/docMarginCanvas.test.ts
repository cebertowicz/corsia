import { afterEach, describe, expect, it } from 'vitest';
import { marginOnCanvas } from './docMarginCanvas';

const settled = () => new Promise<void>((resolve) => setTimeout(resolve));

afterEach(() => { document.body.innerHTML = ''; });

function canvas() {
	const el = document.body.appendChild(document.createElement('div'));
	el.innerHTML = '<div class="cm-editor"><div class="cm-scroller"><div class="cm-content"></div></div></div>';
	const column = document.body.appendChild(document.createElement('section'));
	return { el, column };
}

describe('marginOnCanvas', () => {
	it('moves the column into the editor scroller and marks it on the canvas', () => {
		const { el, column } = canvas();
		marginOnCanvas(column, el);
		expect(column.parentElement).toBe(el.querySelector('.cm-scroller'));
		expect(column.classList.contains('on-canvas')).toBe(true);
	});

	it('waits off the canvas until there is a scroller, then moves in', async () => {
		const el = document.body.appendChild(document.createElement('div'));
		const column = document.body.appendChild(document.createElement('section'));
		marginOnCanvas(column, el);
		expect(column.classList.contains('on-canvas')).toBe(false);
		el.innerHTML = '<div class="cm-scroller"></div>';
		await settled();
		expect(column.parentElement).toBe(el.querySelector('.cm-scroller'));
		expect(column.classList.contains('on-canvas')).toBe(true);
	});

	it('follows the editor into the one that replaces it when the document reloads', async () => {
		const { el, column } = canvas();
		marginOnCanvas(column, el);
		el.innerHTML = '<div class="cm-editor"><div class="cm-scroller" data-next></div></div>';
		await settled();
		expect(column.parentElement).toBe(el.querySelector('[data-next]'));
		expect(column.isConnected).toBe(true);
	});

	it('removes the column when its own branch ends, since it no longer sits in it', () => {
		const { el, column } = canvas();
		const action = marginOnCanvas(column, el);
		action.destroy();
		expect(column.isConnected).toBe(false);
	});
});
