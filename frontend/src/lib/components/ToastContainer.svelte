<script lang="ts">
	import { toasts, type Toast } from '$lib/stores/toast';
	import { toastAnchor } from '$lib/stores/toastAnchor';
	import { toastTop } from '$lib/stores/toastTop';
	import { toastFit } from '$lib/stores/toastFit';
	import { cssVar, ms } from '$lib/popoverMotion';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { flip } from 'svelte/animate';
	import { cubicOut } from 'svelte/easing';
	import { get } from 'svelte/store';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	/** The pinned progress toast leads the stack; ordinary toasts follow below it. */
	const ordered = $derived([...$toasts.filter((toast) => toast.pinned), ...$toasts.filter((toast) => !toast.pinned)]);

	/** Until when a toast is fading out; the toast above waits for that before it glides into the freed place. */
	let leavingUntil = 0;
	const leaveMs = () => ms('--dur-standard') * 0.7;

	/** The upper toast glides into the freed place when the one below it goes, after that one has faded. */
	function flipParams() {
		const reduced =
			typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
		const waiting = !reduced && Date.now() < leavingUntil;
		return { duration: reduced ? 0 : ms('--dur-standard'), delay: waiting ? leaveMs() : 0, easing: cubicOut };
	}

	/**
	 * A toast fades and drops a few pixels as it leaves, at 70% of its entrance (motion.md); reduced
	 * motion keeps the fade only. The leave is `global` so the last toast also plays it when the whole
	 * stack goes.
	 */
	function toastLeave(node: HTMLElement) {
		const duration = leaveMs();
		leavingUntil = Date.now() + duration;
		const from = get(prefersReducedMotion) ? { opacity: 1 } : { opacity: 1, transform: 'none' };
		const to = get(prefersReducedMotion) ? { opacity: 0 } : { opacity: 0, transform: 'translateY(-8px)' };
		node.animate([from, to], { duration, easing: cssVar('--ease-snap') || 'ease-out', fill: 'forwards' });
		return { duration };
	}

	/** Only the pinned slot settles its fit; the parameter re-measures on change. */
	function fitParam(toast: Toast): string | null {
		const pinned = toast.pinned;
		if (!pinned) return null;
		return `${toast.message}|${pinned.detail ?? ''}|${pinned.time ?? ''}|${toast.action?.label ?? ''}`;
	}

	function pinnedFit(node: HTMLElement, param: string | null) {
		if (param === null) return {};
		return toastFit(node, param);
	}

	/** A toast that is itself one control: the notice, or the minimized progress pill. */
	function onPress(toast: Toast): (() => void) | null {
		if (toast.pinned?.compact) return () => toast.pinned?.onExpand?.();
		if (toast.button) return () => void toasts.runAction(toast.id);
		return null;
	}

	function pressKey(event: KeyboardEvent, press: () => void) {
		if (event.target !== event.currentTarget) return;
		if (event.key !== 'Enter' && event.key !== ' ') return;
		event.preventDefault();
		press();
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
			{@const type = toast.type ?? 'info'}
			{@const pinned = toast.pinned}
			{@const press = onPress(toast)}
			<!-- The role and tabindex apply together, only when the whole toast is one control (the notice or the minimized pill). -->
			<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
			<div
				class="toast toast--{type}{pinned ? ' toast--pinned' : ''}{pinned?.compact ? ' toast--compact' : ''}{toast.button ? ' toast--button' : ''}{pinned?.detail ? ' toast--multi' : ''}"
				role={press ? 'button' : pinned ? (type === 'error' ? 'alert' : undefined) : 'status'}
				tabindex={press ? 0 : undefined}
				aria-label={pinned?.compact ? pinned.expandLabel : undefined}
				onclick={press ?? undefined}
				onkeydown={press ? (event) => pressKey(event, press) : undefined}
				data-fit="full"
				animate:flip={flipParams()}
				out:toastLeave|global
				use:pinnedFit={fitParam(toast)}
				onmouseenter={() => toasts.pause(toast.id, 'pointer')}
				onmouseleave={() => toasts.resume(toast.id, 'pointer')}
				onfocusin={() => toasts.pause(toast.id, 'focus')}
				onfocusout={(event) => {
					const target = event.currentTarget as HTMLElement;
					if (!target.contains(event.relatedTarget as Node | null)) {
						toasts.resume(toast.id, 'focus');
					}
				}}
			>
				{#if type === 'progress'}
					<Spinner />
				{:else if type === 'success'}
					<svg class="toast-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="8 12.5 11 15.5 16 9"/></svg>
				{:else if type === 'info' && (pinned || toast.button)}
					<svg class="toast-icon toast-icon--update" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 16V8M8 12l4-4 4 4"/></svg>
				{:else if type === 'error'}
					<svg class="toast-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
				{/if}
				<span class="toast-line" class:toast-line--detail={Boolean(pinned?.detail)}>
					<span class="toast-text" role={pinned && type !== 'error' ? 'status' : undefined}>{toast.message}</span>
					{#if pinned?.detail}<span class="toast-detail">{pinned.detail}</span>{/if}
					{#if pinned?.time}
						<span class="toast-time" aria-hidden="true">{pinned.time}</span>
					{/if}
				</span>
				{#if toast.action && !press}
					<button class="toast-action" onclick={() => void toasts.runAction(toast.id)}>
						{toast.action.label}
					</button>
				{/if}
				{#if !press}
					<button
						class="toast-close"
						onclick={() => { pinned?.onClose?.(); if (pinned?.closeIcon !== 'minimize') toasts.dismiss(toast.id); }}
						aria-label={pinned?.closeLabel ?? 'Dismiss'}
						title={pinned?.closeLabel}>{#if pinned?.closeIcon === 'minimize'}<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>{:else}×{/if}</button>
				{/if}
			</div>
		{/each}
	</div>
{/if}

<style>
	.toast-container {
		/* The column the stack centres on: the chat column in a thread, the main
		   area elsewhere, the window until one registers. */
		--toast-column: min(100vw, var(--toast-anchor-width, 100vw));
		position: fixed;
		/* Under the header and the last status banner. */
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
		/* The stack follows its column as a pane opens or closes, on the
		   drawer's own timing, instead of snapping when the column settles. */
		transition:
			top var(--dur-drawer) var(--ease-ios-sheet),
			left var(--dur-drawer) var(--ease-ios-sheet);
	}

	.toast {
		display: flex;
		align-items: center;
		gap: 10px;
		min-height: 44px;
		padding: var(--space-2) var(--space-4);
		/* Every toast is a pill with the floating controls' rim and elevation; the fill stays opaque so text under it never shows through. */
		border-radius: var(--radius-pill);
		font-size: var(--text-subhead);
		font-weight: 500;
		max-width: calc(100vw - 32px);
		box-sizing: border-box;
		white-space: normal;
		overflow-wrap: anywhere;
		pointer-events: all;
		box-shadow: var(--glass-elevation-1), var(--glass-rim);
		animation: toast-in var(--dur-standard) ease;
	}

	.toast--info,
	.toast--progress,
	.toast--success {
		background: var(--bg-secondary);
		color: var(--text-primary);
		border: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
	}
	.toast--success .toast-icon { color: var(--green-text); }

	.toast--error {
		background: color-mix(in srgb, var(--status-blocked) 15%, var(--bg-secondary));
		color: var(--red-text);
		border: 1px solid color-mix(in srgb, var(--status-blocked) 30%, transparent);
	}

	.toast-icon {
		flex-shrink: 0;
	}

	.toast-icon--update {
		color: var(--accent-text, var(--accent));
	}

	/* A toast with a body: one inset clears the rounded ends, the mark sits on the title line, and Details and close centre on the text block. */
	.toast--multi { padding-inline: var(--space-5); }
	.toast--multi .toast-icon { align-self: flex-start; margin-top: 3px; }

	/* The notice is one button: its text is centred in the whole pill, with no View and no close. */
	.toast--button { cursor: pointer; justify-content: center; text-align: center; }
	.toast--button .toast-line { flex: 0 1 auto; }
	.toast--button:focus-visible,
	.toast--compact:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }

	/* Minimized progress: the mark and its word in a fab-height pill that is one tap target. */
	.toast--pinned.toast--compact {
		min-width: 0;
		align-self: center;
		cursor: pointer;
		font-size: var(--text-footnote);
	}

	.toast-line {
		min-width: 0;
		flex: 1;
	}

	.toast-detail { display: block; color: var(--text-secondary); font-weight: 400; }
	.toast--error .toast-text { color: var(--red-text); font-weight: var(--fw-semibold); }
	.toast--error .toast-action { color: var(--red-text); }
	.toast-time {
		color: var(--text-tertiary);
		font-weight: 400;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.toast-time::before {
		content: '\00a0·\00a0';
	}

	/* A pinned line never ellipsizes. The toastFit action writes `data-fit` after
	   measuring (hence `:global`): the whole line on one row, then without the
	   time, then the step text wrapped. */
	.toast--pinned {
		gap: var(--space-2);
		/* The step text changes length (Stopping, Restarting, Resuming, Updated); holding the
		   widest step's width keeps the toast from resizing, and its flip from stretching text. */
		min-width: min(26rem, calc(var(--toast-column) - var(--space-7)));
	}
	.toast--pinned .toast-line {
		flex: 0 0 auto;
		white-space: nowrap;
	}
	.toast--pinned:global([data-fit='no-time']) .toast-time,
	.toast--pinned:global([data-fit='wrap']) .toast-time {
		display: none;
	}
	.toast--pinned:global([data-fit='wrap']) .toast-line {
		flex: 1 1 auto;
		white-space: normal;
	}
	.toast--pinned .toast-close {
		margin-left: auto;
	}

	.toast-action {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		min-height: 32px;
		background: none;
		border: none;
		border-radius: var(--radius-xs, 4px);
		color: inherit;
		cursor: pointer;
		font: inherit;
		font-weight: 700;
		padding: 0 var(--space-1);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	.toast-action:hover,
	.toast-action:focus-visible { background: var(--bg-hover); }

	.toast-close {
		background: none;
		border: none;
		cursor: pointer;
		color: inherit;
		/* 3:1 against the failure tint in dark mode (0.6 measured 2.95:1). */
		opacity: 0.75;
		font-size: var(--text-callout);
		line-height: 1;
		padding: 0 2px;
		width: 32px;
		height: 32px;
		flex-shrink: 0;
	}
	.toast-close:hover { opacity: 1; }
	@media (max-width: 600px) {
		.toast-container {
			/* The pinned line spans the column, sharing the composer's edges;
			   ordinary toasts keep their own width. */
			width: calc(var(--toast-column) - var(--space-3) * 2);
			max-width: none;
		}
		.toast--pinned {
			align-self: stretch;
			max-width: none;
			/* The 44px close button is the trailing edge; the line keeps its room. */
			padding-right: var(--space-1);
		}
		.toast--pinned.toast--compact { align-self: center; padding-right: var(--space-4); }
		.toast-action { min-height: 44px; }
		.toast-close { width: 44px; height: 44px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.toast-container { transition: none; }
		.toast { animation: none; }
	}

	@keyframes toast-in {
		from { opacity: 0; transform: translateY(-8px); }
		to { opacity: 1; transform: translateY(0); }
	}
</style>
