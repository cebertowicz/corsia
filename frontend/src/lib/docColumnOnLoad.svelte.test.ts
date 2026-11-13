import { afterEach, expect, it } from 'vitest';
import { flushSync } from 'svelte';
import { createCommentSurfaceState, openColumnOnLoad } from './docCommentSurfaceState.svelte';

let dispose: (() => void) | null = null;
afterEach(() => { dispose?.(); dispose = null; });

/** A document whose comments are ready, with the surface's margin rule answering `fits`. */
function create(initiallyFits: boolean) {
	const room = $state({ fits: initiallyFits });
	let ui!: ReturnType<typeof createCommentSurfaceState>;
	dispose = $effect.root(() => {
		ui = createCommentSurfaceState(() => {}, undefined, () => room.fits);
		openColumnOnLoad({ artifactId: () => 'doc', ready: () => true, paneDrawer: () => false, fits: () => room.fits, hasComments: () => true,
			shown: () => !!ui.state, open: ui.openOnLoad, quietList: () => ui.state?.kind === 'list' && ui.quiet, close: ui.closeAll });
	});
	flushSync();
	return { room, ui };
}

it('shows the column on load only where the surface says it fits', () => {
	expect(create(true).ui.state).toEqual({ kind: 'list' });
	dispose?.();
	expect(create(false).ui.state).toBeNull();
});

it('closes a list nobody opened once the column stops fitting, and keeps it closed when room comes back', () => {
	const { room, ui } = create(true);
	room.fits = false;
	flushSync();
	expect(ui.state).toBeNull();
	room.fits = true;
	flushSync();
	expect(ui.state).toBeNull();
});

it('leaves a list the reader opened to the surface when the column stops fitting', () => {
	const { room, ui } = create(false);
	ui.openList(document.createElement('button'));
	room.fits = false;
	flushSync();
	expect(ui.state).toEqual({ kind: 'list' });
});
