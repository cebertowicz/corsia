import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, tick } from 'svelte';
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
	const popover = $state<{ el: HTMLElement | null }>({ el: null });
	let reveal!: ReturnType<typeof createChatSheetReveal>;
	stop = $effect.root(() => { reveal = createChatSheetReveal(() => panel.el, () => mode.now, () => 'c1', () => popover.el); });
	return { reveal, panel, mode, popover };
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

describe('createChatSheetReveal focus', () => {
	it('moves focus to the Back button inside its own sheet, not to a document comment view\'s `.thread-back` wrapper', async () => {
		vi.stubGlobal('requestAnimationFrame', (run: FrameRequestCallback) => { run(0); return 0; });
		const docWrapper = document.createElement('div');
		docWrapper.className = 'thread-back';
		docWrapper.tabIndex = -1;
		document.body.append(docWrapper);
		const { panel, mode } = setup('list');
		flushSync();
		const sheet = document.createElement('div');
		const back = document.createElement('button');
		back.className = 'thread-back';
		sheet.append(back);
		document.body.append(sheet);
		panel.el = sheet;
		mode.now = 'thread'; flushSync();
		await tick(); await tick();
		expect(document.activeElement).toBe(back);
	});

	it('moves focus to the Back button inside the wide popover, where there is no sheet panel, not to a document view\'s wrapper', async () => {
		vi.stubGlobal('requestAnimationFrame', (run: FrameRequestCallback) => { run(0); return 0; });
		const docWrapper = document.createElement('div');
		docWrapper.className = 'thread-back';
		docWrapper.tabIndex = -1;
		document.body.append(docWrapper);
		const { mode, popover } = setup('list');
		flushSync();
		const dialog = document.createElement('div');
		const back = document.createElement('button');
		back.className = 'thread-back';
		dialog.append(back);
		document.body.append(dialog);
		popover.el = dialog;
		mode.now = 'thread'; flushSync();
		await tick(); await tick();
		expect(document.activeElement).toBe(back);
	});
});
