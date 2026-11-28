<script lang="ts">
	import type { Snippet } from 'svelte';
	// The Comment button is the document's own (the one the editor floats beside a selection); this only holds it at the bottom of the visible screen.
	let { column, offered, above = 0, children }: { column: HTMLElement | undefined; offered: boolean; above?: number; children: Snippet } = $props();
	let bar = $state<HTMLDivElement>();
	let bottom = $state(0);
	let left = $state(0);
	let width = $state(0);
	$effect(() => {
		const root = column, element = bar;
		// Measured again each time the offer comes or goes: the column can move (a page or panel sliding in) without resizing, and the bar must not keep an earlier place.
		void offered;
		const reach = above;
		if (!root || !element) return;
		const viewport = window.visualViewport;
		const update = () => {
			const bounds = root.getBoundingClientRect();
			left = bounds.left;
			width = bounds.width;
			const lift = Math.max(0, window.innerHeight - ((viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight)));
			// Held above whatever stands at the canvas's foot (the ask card), not over its buttons; the keyboard still wins when it is higher.
			// The card reaches the canvas's padded foot, so its top is its reach above the canvas box plus that padding.
			const clear = reach > 0 ? reach + (Number.parseFloat(getComputedStyle(root).getPropertyValue('--canvas-pad-bottom')) || 0) : 0;
			// The same rule for the jump control the canvas shows at its foot (the canvas marks it `data-notice-jump`): where it would sit under the button's width, the button is held just above it.
			const canvas = root.closest('.doc');
			const jump = canvas?.hasAttribute('data-notice-jump') ? canvas.querySelector('[data-doc-jump] button') : null;
			const face = element.firstElementChild?.getBoundingClientRect();
			const jumpRect = jump?.getBoundingClientRect();
			const jumpClear = jumpRect && face && jumpRect.width > 0 && face.width > 0 && jumpRect.left < face.right && face.left < jumpRect.right ? window.innerHeight - jumpRect.top : 0;
			bottom = Math.max(lift, clear, jumpClear);
			// The room is what the button covers of the document's column, never more: a button on a column that ends above the page's foot covers less than its own height.
			// The holder's top is worked out from the lift just set, not read back: the lift reaches the page at the next flush, so a measured top is the one before the move.
			const height = element.getBoundingClientRect().height;
			root.style.setProperty('--document-comment-room', `${Math.max(0, Math.min(height + Math.max(0, bottom - lift), bounds.bottom - (window.innerHeight - bottom - height)))}px`);
		};
		update();
		const observer = new ResizeObserver(update);
		observer.observe(element);
		observer.observe(root);
		// The canvas marks and unmarks its jump control by an attribute, which no resize or scroll follows.
		const canvasMarks = root.closest('.doc');
		const marks = canvasMarks ? new MutationObserver(update) : null;
		if (canvasMarks) marks?.observe(canvasMarks, { attributes: true, attributeFilter: ['data-notice-jump'] });
		viewport?.addEventListener('resize', update);
		viewport?.addEventListener('scroll', update);
		window.addEventListener('resize', update);
		window.addEventListener('scroll', update, true);
		// A column sliding into place moves without a resize or a scroll: its transition or animation ending is the moment it has arrived.
		window.addEventListener('transitionend', update, true);
		window.addEventListener('animationend', update, true);
		return () => {
			observer.disconnect();
			marks?.disconnect();
			viewport?.removeEventListener('resize', update);
			viewport?.removeEventListener('scroll', update);
			window.removeEventListener('resize', update);
			window.removeEventListener('scroll', update, true);
			window.removeEventListener('transitionend', update, true);
			window.removeEventListener('animationend', update, true);
			root.style.removeProperty('--document-comment-room');
		};
	});
</script>

<div class="touch-comment-bar" class:offered bind:this={bar} style:bottom="{bottom}px" style:left="{left}px" style:width="{width}px" aria-hidden={!offered}>
	{#if offered}{@render children()}{/if}
</div>

<style>
	/* A holder only: no fill, border or band. It keeps the button centred at the bottom of the visible screen, above the keyboard and the safe-area inset, and its height is the room the button covers. */
	.touch-comment-bar { position: fixed; z-index: 5; display: flex; justify-content: center; box-sizing: border-box; min-height: calc(var(--touch-target) + max(var(--space-2), env(safe-area-inset-bottom))); padding-bottom: max(var(--space-2), env(safe-area-inset-bottom)); pointer-events: none; }
	.touch-comment-bar :global(.sel-comment) { position: static; translate: none; align-self: flex-end; pointer-events: auto; }
</style>
