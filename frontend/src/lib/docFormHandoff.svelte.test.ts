import { afterEach, expect, it } from 'vitest';
import { flushSync, tick } from 'svelte';
import { createFormHandoff } from './docFormHandoff.svelte';
import { writingField } from './docCommentDrawer';

let dispose: (() => void) | null = null;
afterEach(() => { dispose?.(); dispose = null; document.body.replaceChildren(); });

const reply = (thread: string) => `<div data-thread-id="${thread}"><textarea class="ct-input"></textarea></div>`;

/** A surface showing thread `a` in `form`; the reader's fields are whatever the body holds. */
function create(form = 'popover') {
	const shown = $state({ surface: 'thread:a', form, drawer: undefined as HTMLElement | undefined });
	let handoff!: ReturnType<typeof createFormHandoff>;
	dispose = $effect.root(() => {
		handoff = createFormHandoff({ surfaceKey: () => shown.surface, formKey: () => shown.form, drawer: () => shown.drawer,
			writing: (active) => writingField(active, [{ id: 'a' }, { id: 'b' }], false) });
	});
	flushSync();
	return { shown, handoff };
}

it('treats the first form of a surface as the reader opening it: no redraw, nothing handed off', () => {
	document.body.innerHTML = reply('a');
	document.querySelector<HTMLElement>('.ct-input')!.focus();
	const { handoff } = create();
	expect(handoff.redrawn).toBe(false);
	expect(handoff.handoff).toBeNull();
});

it('hands the reply the reader is writing in to the same field in the redrawn form', () => {
	document.body.innerHTML = reply('a');
	const { shown, handoff } = create();
	document.querySelector<HTMLElement>('.ct-input')!.focus();
	shown.form = 'sheet';
	flushSync();
	expect(handoff.redrawn).toBe(true);
	expect(handoff.handoff).toBe('[data-thread-id="a"] .ct-input');
});

it('hands nothing off when focus is not in one of the surface\'s comment fields', () => {
	document.body.innerHTML = `${reply('a')}<button>Elsewhere</button>`;
	const { shown, handoff } = create();
	document.querySelector<HTMLElement>('button')!.focus();
	shown.form = 'sheet';
	flushSync();
	expect(handoff.redrawn).toBe(true);
	expect(handoff.handoff).toBeNull();
});

it('uses the field noted at the width when the old form took the focused field with it', () => {
	document.body.innerHTML = reply('a');
	const { shown, handoff } = create();
	document.querySelector<HTMLElement>('.ct-input')!.focus();
	handoff.note();
	document.body.replaceChildren();
	shown.form = 'sheet';
	flushSync();
	expect(handoff.handoff).toBe('[data-thread-id="a"] .ct-input');
	// Consumed by that redraw: the next one, with focus still gone, has nothing noted.
	shown.form = 'popover';
	flushSync();
	expect(handoff.handoff).toBeNull();
});

it('lets a later width overwrite the note: focus moved on before the redraw is not handed back', () => {
	document.body.innerHTML = `${reply('a')}<button>Elsewhere</button>`;
	const { shown, handoff } = create();
	document.querySelector<HTMLElement>('.ct-input')!.focus();
	handoff.note();
	document.querySelector<HTMLElement>('button')!.focus();
	handoff.note();
	document.querySelector('button')!.remove();
	shown.form = 'sheet';
	flushSync();
	expect(handoff.handoff).toBeNull();
});

it('focuses the handed-off reply once in the margin column, and not over a field the reader moved to', async () => {
	document.body.innerHTML = `${reply('a')}<div class="col">${reply('a')}</div>${reply('b')}`;
	const [old, , other] = [...document.querySelectorAll<HTMLElement>('.ct-input')];
	const column = document.querySelector<HTMLElement>('.col')!;
	const { shown, handoff } = create();
	old.focus();
	shown.form = 'margin';
	shown.drawer = column;
	flushSync();
	await tick();
	expect(document.activeElement).toBe(column.querySelector('.ct-input'));
	expect(handoff.handoff).toBeNull();

	old.focus();
	shown.form = 'popover';
	flushSync();
	shown.form = 'margin';
	flushSync();
	other.focus();
	await tick();
	expect(document.activeElement).toBe(other);
});

it('waits for a margin card still hidden for want of a place, then focuses its reply once it is placed', async () => {
	document.body.innerHTML = `${reply('a')}<div class="col"><div class="margin-card" data-margin-card="a">${reply('a')}</div></div>`;
	const old = document.querySelector<HTMLElement>('.ct-input')!;
	const column = document.querySelector<HTMLElement>('.col')!;
	const { shown } = create();
	old.focus();
	shown.form = 'margin';
	shown.drawer = column;
	flushSync();
	old.closest('[data-thread-id]')!.remove();
	await tick();
	// A browser drops focus on a hidden field; it is not tried until the card has its place.
	expect(document.activeElement).toBe(document.body);
	column.querySelector('[data-margin-card]')!.classList.add('placed');
	await Promise.resolve();
	expect(document.activeElement).toBe(column.querySelector('.ct-input'));
});

it('takes no focus on placement once the reader has moved on', async () => {
	document.body.innerHTML = `${reply('a')}<div class="col"><div class="margin-card" data-margin-card="a">${reply('a')}</div></div><button>Elsewhere</button>`;
	const old = document.querySelector<HTMLElement>('.ct-input')!;
	const column = document.querySelector<HTMLElement>('.col')!;
	const { shown } = create();
	old.focus();
	shown.form = 'margin';
	shown.drawer = column;
	flushSync();
	await tick();
	document.querySelector<HTMLElement>('button')!.focus();
	column.querySelector('[data-margin-card]')!.classList.add('placed');
	await Promise.resolve();
	expect(document.activeElement).toBe(document.querySelector('button'));
});
