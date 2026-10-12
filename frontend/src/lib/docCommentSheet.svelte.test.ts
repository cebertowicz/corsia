import { flushSync } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import { createCommentSheet, type SheetDraft } from './docCommentSheet.svelte';
import type { CommentSurfaceState } from './docCommentSurfaceState.svelte';

function setup(initial: { state?: CommentSurfaceState; sheetDrawer?: boolean; narrow?: boolean } = {}) {
	let state = $state<CommentSurfaceState>(initial.state ?? null);
	let draft = $state<SheetDraft>(null);
	let sheetDrawer = $state(initial.sheetDrawer ?? true);
	let narrow = $state(initial.narrow ?? true);
	const onClose = vi.fn();
	const onCancelDraft = vi.fn();
	let sheet!: ReturnType<typeof createCommentSheet>;
	const teardown = $effect.root(() => {
		sheet = createCommentSheet({
			state: () => state,
			draft: () => draft,
			sheetDrawer: () => sheetDrawer,
			narrow: () => narrow,
			onClose,
			onCancelDraft,
		});
	});
	flushSync();
	return {
		get sheet() { return sheet; },
		onClose,
		onCancelDraft,
		teardown,
		set: (next: { state?: CommentSurfaceState; draft?: SheetDraft; sheetDrawer?: boolean; narrow?: boolean }) => {
			if ('state' in next) state = next.state ?? null;
			if ('draft' in next) draft = next.draft ?? null;
			if (next.sheetDrawer !== undefined) sheetDrawer = next.sheetDrawer;
			if (next.narrow !== undefined) narrow = next.narrow;
			flushSync();
		},
	};
}

describe('createCommentSheet', () => {
	it('opens when the surface has a state and the sheet form applies', () => {
		const t = setup();
		expect(t.sheet.open).toBe(false);
		t.set({ state: { kind: 'list' } });
		expect(t.sheet.open).toBe(true);
		expect(t.sheet.state).toEqual({ kind: 'list' });
		t.teardown();
	});

	it('stays closed outside the sheet form', () => {
		const t = setup({ sheetDrawer: false });
		t.set({ state: { kind: 'list' } });
		expect(t.sheet.open).toBe(false);
		t.teardown();
	});

	it('runs the close only after the drawer outro finishes', () => {
		const t = setup({ state: { kind: 'list' } });
		t.sheet.close();
		flushSync();
		expect(t.sheet.open).toBe(false);
		expect(t.onClose).not.toHaveBeenCalled();
		t.sheet.finishClose();
		expect(t.onClose).toHaveBeenCalledOnce();
		t.teardown();
	});

	it('closes at once outside the sheet form', () => {
		const t = setup({ state: { kind: 'list' }, sheetDrawer: false });
		t.sheet.close();
		expect(t.onClose).toHaveBeenCalledOnce();
		t.teardown();
	});

	it('keeps a cancelled draft on screen through its outro on a narrow canvas', () => {
		const t = setup({ narrow: true });
		t.set({ state: { kind: 'draft' }, draft: { text: 'quote', body: 'typed' } });
		t.sheet.cancelDraft();
		expect(t.onCancelDraft).toHaveBeenCalledOnce();
		t.set({ state: null, draft: null });
		expect(t.sheet.open).toBe(false);
		expect(t.sheet.closing).toBe(true);
		expect(t.sheet.state).toEqual({ kind: 'draft' });
		expect(t.sheet.draft).toEqual({ text: 'quote', body: 'typed' });
		t.sheet.finishClose();
		flushSync();
		expect(t.sheet.closing).toBe(false);
		expect(t.sheet.state).toBeNull();
		expect(t.onClose).not.toHaveBeenCalled();
		t.teardown();
	});

	it('cancels a draft without an outro on a wide canvas', () => {
		const t = setup({ narrow: false });
		t.set({ state: { kind: 'draft' }, draft: { text: 'quote' } });
		t.sheet.cancelDraft();
		expect(t.onCancelDraft).toHaveBeenCalledOnce();
		expect(t.sheet.closing).toBe(false);
		t.teardown();
	});

	it('drops held closing content when the sheet reopens', () => {
		const t = setup({ narrow: true });
		t.set({ state: { kind: 'draft' }, draft: { text: 'quote' } });
		t.sheet.cancelDraft();
		t.set({ state: null, draft: null });
		t.set({ state: { kind: 'list' } });
		expect(t.sheet.open).toBe(true);
		expect(t.sheet.closing).toBe(false);
		expect(t.sheet.state).toEqual({ kind: 'list' });
		t.teardown();
	});
});
