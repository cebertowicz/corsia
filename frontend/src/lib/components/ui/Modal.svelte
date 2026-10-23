<script lang="ts">
	import { fade } from 'svelte/transition';
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

	// `fly` is a rise plus a fade. The blur has to sit on an element with no faded or
	// filtered ancestor, or the browser makes that ancestor a backdrop root and the blur
	// samples nothing. So the rise is on `.modal-frame` (transform only), and the fade is
	// on `.modal` and, separately, on the glass layer beside it. Same duration and easing
	// as `fly`, so the card's opacity and position match it frame for frame.
	function modalRise(_node: Element, { y = 12, duration = 380, easing = backOut } = {}) {
		return {
			duration,
			easing,
			css: (_t: number, u: number) => `transform: translateY(${u * y}px)`,
		};
	}
	function modalFade(_node: Element, { duration = 380, easing = backOut } = {}) {
		return { duration, easing, css: (t: number) => `opacity: ${t}` };
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
<div class="modal-layer" use:portal>
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="bg" onclick={onBackdropClick} transition:fade={{ duration: 120 }}></div>
	<div class="modal-frame {size}" transition:modalRise={{ y: 12, duration: 380, easing: backOut }}>
		<div class="modal-glass" transition:modalFade={{ duration: 380, easing: backOut }}></div>
		<div
			bind:this={modalElement}
			class="modal {size}"
			transition:modalFade={{ duration: 380, easing: backOut }}
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
</div>

<style>
	.modal-layer {
		position: fixed; inset: 0;
		z-index: 100;
		display: flex; align-items: center; justify-content: center;
		padding-top:    max(32px, var(--safe-top) + 8px);
		padding-bottom: max(32px, env(safe-area-inset-bottom, 0px) + 8px);
		padding-left: 32px; padding-right: 32px;
		pointer-events: none;
	}
	.bg {
		position: absolute; inset: 0;
		background: var(--glass-scrim);
		pointer-events: auto;
	}
	/* Sizes and rises the card. Carries only a transform animation, never opacity. */
	.modal-frame {
		position: relative;
		z-index: 1;
		pointer-events: auto;
		display: flex; flex-direction: column;
		width: 100%;
		max-height: 86vh;
		max-height: 86dvh;
	}
	.modal-frame.sm { max-width: 480px; }
	.modal-frame.md { max-width: 640px; }
	.modal-frame.lg { max-width: 800px; }
	/*
		The glass: tint plus blur, on its own element beside the card, so no ancestor of it
		carries opacity, filter or mask (the frame animates transform only). It reaches three
		sigma past the card so the blur's sampling edge is hidden, and its own clip-path trims
		it back to the card's rounded box. Its own opacity fades it with the card.
	*/
	.modal-glass {
		position: absolute;
		inset: calc(-1 * var(--glass-blur-reach));
		clip-path: inset(var(--glass-blur-reach) round var(--radius));
		pointer-events: none;
		background: var(--glass-regular-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
	}
	.modal {
		position: relative;
		border: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
		border-radius: var(--radius);
		box-shadow: var(--glass-elevation-3);
		display: flex; flex-direction: column; overflow: hidden;
		min-height: 0;
		width: 100%;
	}

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
		.modal-layer {
			padding-top:    max(12px, var(--safe-top) + 4px);
			padding-bottom: max(12px, env(safe-area-inset-bottom, 0px) + 4px);
			padding-left: 12px; padding-right: 12px;
		}
		/* Fill only the room inside the safe-area padding, so the footer clears the home indicator. */
		.modal-frame { max-height: 100%; }
	}
	@media (max-width: 480px) {
		.modal-layer {
			padding-top:    max(8px, var(--safe-top));
			padding-bottom: max(8px, env(safe-area-inset-bottom, 0px));
			padding-left: 8px; padding-right: 8px;
		}
		.head { padding: 14px 16px; }
		.body { padding: 14px 16px; }
		.foot { padding: 12px 16px; }
	}
</style>
