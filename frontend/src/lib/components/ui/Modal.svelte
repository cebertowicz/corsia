<script lang="ts">
	import { fade, fly } from 'svelte/transition';
	import { backOut } from 'svelte/easing';
	import { onMount, tick } from 'svelte';
	import type { Snippet } from 'svelte';
	import { portal } from '$lib/actions/portal';
	import IconButton from './IconButton.svelte';

	interface Props {
		title?: string;
		size?: 'sm' | 'md' | 'lg';
		role?: 'dialog' | 'alertdialog';
		closeOnEscape?: boolean;
		closeOnBackdrop?: boolean;
		ariaLabel?: string;
		ariaLabelledBy?: string;
		onClose: () => void;
		head?: Snippet;
		foot?: Snippet;
		children: Snippet;
	}

	const {
		title, size = 'md', role = 'dialog',
		closeOnEscape = true, closeOnBackdrop = true,
		ariaLabel, ariaLabelledBy,
		onClose, head, foot, children,
	}: Props = $props();

	let modalElement: HTMLElement;

	function focusableElements(root: HTMLElement | undefined = modalElement): HTMLElement[] {
		if (!root) return [];
		return Array.from(root.querySelectorAll<HTMLElement>(
			'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), '
			+ 'textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
		));
	}

	function onKeydown(e: KeyboardEvent) {
		if (closeOnEscape && e.key === 'Escape') onClose();
		if (e.key !== 'Tab') return;

		const focusable = focusableElements();
		if (!focusable.length) {
			e.preventDefault();
			modalElement.focus();
			return;
		}

		const first = focusable[0];
		const last = focusable.at(-1)!;
		const current = document.activeElement;
		if (e.shiftKey && (current === first || !modalElement.contains(current))) {
			e.preventDefault();
			last.focus();
		} else if (!e.shiftKey && (current === last || !modalElement.contains(current))) {
			e.preventDefault();
			first.focus();
		}
	}

	function onBackdropClick(e: MouseEvent) {
		if (!closeOnBackdrop) return;
		if (e.target === e.currentTarget) onClose();
	}

	onMount(() => {
		const previousFocus = document.activeElement instanceof HTMLElement
			? document.activeElement
			: null;
		const element = modalElement;
		let mounted = true;
		void tick().then(() => {
			if (!mounted || !element.isConnected) return;
			(focusableElements(element)[0] ?? element).focus();
		});

		return () => {
			mounted = false;
			const activeElement = document.activeElement;
			if (
				previousFocus?.isConnected
				&& (
					activeElement === document.body
					|| element.contains(activeElement)
				)
			) {
				previousFocus.focus();
			}
		};
	});
</script>

<!--
	Portaled to `document.body`, like AdaptiveDrawer and Menu. Mounted inside the chat
	column or the open document, the scrim and card were clipped to that column in a
	WebKit webview and sat behind the other panes.
-->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="bg" onclick={onBackdropClick} transition:fade={{ duration: 120 }} use:portal>
	<div
		bind:this={modalElement}
		class="modal {size}"
		transition:fly={{ y: 12, duration: 380, easing: backOut }}
		{role}
		aria-modal="true"
		aria-label={ariaLabel}
		aria-labelledby={ariaLabelledBy}
		tabindex="-1"
		onkeydown={onKeydown}
	>
		{#if head}
			{@render head()}
		{:else if title}
			<header class="head">
				<div class="title">{title}</div>
				<IconButton ariaLabel="Close" onclick={onClose}>
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6 18 18M18 6 6 18"/></svg>
				</IconButton>
			</header>
		{/if}

		<div class="body">
			{@render children()}
		</div>

		{#if foot}
			<footer class="foot">
				{@render foot()}
			</footer>
		{/if}
	</div>
</div>

<style>
	.bg {
		position: fixed; inset: 0;
		background: var(--glass-scrim);
		z-index: 100;
		display: flex; align-items: center; justify-content: center;
		padding-top:    max(32px, var(--safe-top) + 8px);
		padding-bottom: max(32px, env(safe-area-inset-bottom, 0px) + 8px);
		padding-left: 32px; padding-right: 32px;
	}
	.modal {
		background: var(--glass-regular-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		border: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
		border-radius: var(--radius);
		box-shadow: var(--glass-elevation-3);
		display: flex; flex-direction: column; overflow: hidden;
		width: 100%;
		max-height: 86vh;
		max-height: 86dvh;
	}
	.modal.sm { max-width: 480px; }
	.modal.md { max-width: 640px; }
	.modal.lg { max-width: 800px; }

	.head {
		display: flex; align-items: center; gap: 12px;
		padding: 18px 22px;
	}
	.title {
		font-size: var(--text-body); font-weight: 600; flex: 1;
		display: flex; align-items: center; gap: 10px;
		letter-spacing: -0.01em;
		color: var(--text-primary);
	}

	.body { padding: 18px 22px; overflow-y: auto; flex: 1; }

	.foot {
		display: flex; align-items: center; justify-content: flex-end; gap: 8px;
		padding: 14px 22px;
	}

	@media (max-width: 720px) {
		.bg {
			padding-top:    max(12px, var(--safe-top) + 4px);
			padding-bottom: max(12px, env(safe-area-inset-bottom, 0px) + 4px);
			padding-left: 12px; padding-right: 12px;
		}
		/* Fill only the room inside the safe-area padding, so the footer clears the home indicator. */
		.modal { max-height: 100%; }
	}
	@media (max-width: 480px) {
		.bg {
			padding-top:    max(8px, var(--safe-top));
			padding-bottom: max(8px, env(safe-area-inset-bottom, 0px));
			padding-left: 8px; padding-right: 8px;
		}
		.head { padding: 14px 16px; }
		.body { padding: 14px 16px; }
		.foot { padding: 12px 16px; }
	}
</style>
