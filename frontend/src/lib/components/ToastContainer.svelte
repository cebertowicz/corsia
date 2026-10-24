<script lang="ts">
	import { toasts } from '$lib/stores/toast';
	import { toastAnchor } from '$lib/stores/toastAnchor';
	import { toastTop } from '$lib/stores/toastTop';
	import { flip } from 'svelte/animate';
	import { cubicOut } from 'svelte/easing';
	import ToastNotice from './ToastNotice.svelte';

	/** The pinned progress toast leads the stack; ordinary toasts follow below it. */
	const ordered = $derived([...$toasts.filter((toast) => toast.pinned), ...$toasts.filter((toast) => !toast.pinned)]);
	function flipParams() {
		const reduced = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
		return { duration: reduced ? 0 : 180, easing: cubicOut };
	}
</script>

{#if $toasts.length > 0}
	<div
		class="toast-container"
		aria-live="polite"
		aria-atomic="false"
		style:--toast-top="{$toastTop}px"
		style:--toast-anchor-x={$toastAnchor ? `${$toastAnchor.x}px` : undefined}
		style:--toast-anchor-width={$toastAnchor ? `${$toastAnchor.width}px` : undefined}
	>
		{#each ordered as toast (toast.id)}
			<div class="toast-motion" animate:flip={flipParams()}><ToastNotice {toast} /></div>
		{/each}
	</div>
{/if}

<style>
	.toast-container {
		/* The column the stack centres on: the chat column in a thread, the main area elsewhere, the window until one registers. */
		--toast-column: min(100vw, var(--toast-anchor-width, 100vw));
		position: fixed;
		top: calc(var(--toast-top, 0px) + var(--space-2));
		left: var(--toast-anchor-x, 50%);
		transform: translateX(-50%);
		width: max-content;
		max-width: calc(var(--toast-column) - var(--space-7));
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-2);
		z-index: 9999;
		pointer-events: none;
		transition: top var(--dur-drawer) var(--ease-ios-sheet), left var(--dur-drawer) var(--ease-ios-sheet);
	}
	.toast-motion { max-width: 100%; pointer-events: all; }
	@media (max-width: 600px) {
		.toast-container { width: calc(var(--toast-column) - var(--space-3) * 2); max-width: none; }
	}
	@media (prefers-reduced-motion: reduce) { .toast-container { transition: none; } }
</style>
