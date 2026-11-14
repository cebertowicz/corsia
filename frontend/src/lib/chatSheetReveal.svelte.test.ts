import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { createChatSheetReveal } from './chatSheetReveal.svelte';

const rect = (top: number, height: number) => new DOMRect(10, top, 200, height);
let stop: (() => void) | null = null;

function span(rows: DOMRect[]): HTMLElement {
	const el = document.createElement('span');
	el.getClientRects = () => rows as unknown as DOMRectList;
	document.body.append(el);
	return el;
}

function setup(initialMode: 'list' | 'thread' | null = null) {
	const panel = $state<{ el: HTMLElement | null }>({ el: null });
	const mode = $state<{ now: 'list' | 'thread' | null }>({ now: initialMode });
	let reveal!: ReturnType<typeof createChatSheetReveal>;
	stop = $effect.root(() => { reveal = createChatSheetReveal(() => panel.el, () => mode.now, () => 'c1'); });
	return { reveal, panel, mode };
}

afterEach(() => { stop?.(); stop = null; document.body.replaceChildren(); vi.unstubAllGlobals(); });

describe('createChatSheetReveal pressedText', () => {
	it('is the whole highlight when it is up to three lines', () => {
		const { reveal } = setup();
		reveal.notePress(span([rect(100, 20), rect(120, 20)]), 1);
		expect(reveal.pressedText()).toMatchObject({ top: 100, height: 40 });
	});

	it('is only the pressed line of a taller highlight', () => {
		const { reveal } = setup();
		reveal.notePress(span([rect(100, 20), rect(120, 20), rect(140, 20), rect(160, 20)]), 2);
		expect(reveal.pressedText()).toMatchObject({ top: 140, height: 20 });
	});

	it('is nothing before a press or once the pressed text is gone', () => {
		const { reveal } = setup();
		expect(reveal.pressedText()).toBeNull();
		const el = span([rect(100, 20)]);
		reveal.notePress(el, 0);
		el.remove();
		expect(reveal.pressedText()).toBeNull();
	});

	it('forgets the press when its sheet closes, so a comment opened another way is not placed from it', () => {
		const { reveal, panel } = setup();
		const sheet = document.createElement('div');
		panel.el = sheet; flushSync();
		reveal.notePress(span([rect(100, 20)]), 0);
		expect(reveal.pressedText()).not.toBeNull();
		panel.el = null; flushSync();
		expect(reveal.pressedText()).toBeNull();
	});
});
