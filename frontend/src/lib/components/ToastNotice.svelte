<script module lang="ts">
	import { ms } from '$lib/popoverMotion';

	/** How long a notice takes to fade out: 70% of its entrance (motion.md). The stack waits this long before it glides. */
	export const toastLeaveMs = () => ms('--dur-standard') * 0.7;
</script>

<script lang="ts">
	import { toasts, type Toast } from '$lib/stores/toast';
	import { toastFit } from '$lib/stores/toastFit';
	import { cssVar } from '$lib/popoverMotion';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { get } from 'svelte/store';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	let { toast, onDismiss, onLeave, animateOut = true }: { toast: Toast; onDismiss?: () => void; onLeave?: (until: number) => void; animateOut?: boolean } = $props();
	const type = $derived(toast.type ?? 'info');
	const pinned = $derived(toast.pinned);
	const press = $derived(onPress(toast));

	/**
	 * A notice fades and drops a few pixels as it leaves, at 70% of its entrance (motion.md); reduced
	 * motion keeps the fade only. The leave is `global` so the last notice also plays it when the whole
	 * stack goes. `onLeave` tells the stack until when this one is fading, so the notice above waits.
	 */
	function toastLeave(node: HTMLElement) {
		if (!animateOut) return { duration: 0 };
		const duration = toastLeaveMs();
		onLeave?.(Date.now() + duration);
		const from = get(prefersReducedMotion) ? { opacity: 1 } : { opacity: 1, transform: 'none' };
		const to = get(prefersReducedMotion) ? { opacity: 0 } : { opacity: 0, transform: 'translateY(-8px)' };
		node.animate([from, to], { duration, easing: cssVar('--ease-snap') || 'ease-out', fill: 'forwards' });
		return { duration };
	}

	function fitParam(current: Toast): string | null {
		const currentPinned = current.pinned;
		if (!currentPinned) return null;
		return `${current.message}|${currentPinned.detail ?? ''}|${currentPinned.time ?? ''}|${current.action?.label ?? ''}`;
	}
	function pinnedFit(node: HTMLElement, param: string | null) { return param === null ? {} : toastFit(node, param); }
	function onPress(current: Toast): (() => void) | null {
		if (current.pinned?.compact) return () => current.pinned?.onExpand?.();
		if (current.button) return () => void toasts.runAction(current.id);
		return null;
	}
	function pressKey(event: KeyboardEvent, callback: () => void) {
		if (event.target !== event.currentTarget || (event.key !== 'Enter' && event.key !== ' ')) return;
		event.preventDefault();
		callback();
	}
	function dismiss() {
		pinned?.onClose?.();
		if (pinned?.closeIcon !== 'minimize') {
			if (onDismiss) onDismiss();
			else toasts.dismiss(toast.id);
		}
	}
</script>

<!-- The one reusable visual for global and canvas-owned notices. Placement belongs to the calling surface. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div
	class="toast toast--{type}{pinned ? ' toast--pinned' : ''}{pinned?.compact ? ' toast--compact' : ''}{toast.button ? ' toast--button' : ''}{pinned?.detail ? ' toast--multi' : ''}"
	role={press ? 'button' : pinned ? (type === 'error' ? 'alert' : undefined) : 'status'}
	tabindex={press ? 0 : undefined}
	aria-label={pinned?.compact ? pinned.expandLabel : undefined}
	onclick={() => press?.()}
	onkeydown={(event) => { if (press) pressKey(event, press); }}
	data-fit="full"
	out:toastLeave|global
	use:pinnedFit={fitParam(toast)}
	onmouseenter={() => toasts.pause(toast.id, 'pointer')}
	onmouseleave={() => toasts.resume(toast.id, 'pointer')}
	onfocusin={() => toasts.pause(toast.id, 'focus')}
	onfocusout={(event) => {
		const target = event.currentTarget as HTMLElement;
		if (!target.contains(event.relatedTarget as Node | null)) toasts.resume(toast.id, 'focus');
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
		{#if pinned?.time}<span class="toast-time" aria-hidden="true">{pinned.time}</span>{/if}
	</span>
	{#if toast.action && !press}<button class="toast-action" onclick={() => void toasts.runAction(toast.id)}>{toast.action.label}</button>{/if}
	{#if !press}
		<button class="toast-close" onclick={dismiss} aria-label={pinned?.closeLabel ?? 'Dismiss'} title={pinned?.closeLabel}>{#if pinned?.closeIcon === 'minimize'}<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>{:else}×{/if}</button>
	{/if}
</div>

<style>
	.toast { display: flex; align-items: center; gap: var(--button-pad-x-sm); min-height: var(--touch-target); padding: var(--space-2) var(--space-4); border-radius: var(--radius-pill); font-size: var(--text-subhead); font-weight: 500; max-width: calc(100vw - var(--space-7)); box-sizing: border-box; white-space: normal; overflow-wrap: anywhere; pointer-events: all; box-shadow: var(--glass-elevation-1), var(--glass-rim); animation: toast-in var(--dur-standard) ease; }
	.toast--info, .toast--progress, .toast--success { background: var(--bg-secondary); color: var(--text-primary); border: 1px solid color-mix(in srgb, var(--border) 72%, transparent); }
	.toast--success .toast-icon { color: var(--green-text); }
	.toast--error { background: color-mix(in srgb, var(--status-blocked) 15%, var(--bg-secondary)); color: var(--red-text); border: 1px solid color-mix(in srgb, var(--status-blocked) 30%, transparent); }
	.toast-icon { flex-shrink: 0; }
	.toast-icon--update { color: var(--accent-text, var(--accent)); }
	.toast--multi { padding-inline: var(--space-5); }
	.toast--multi .toast-icon { align-self: flex-start; margin-top: var(--toast-detail-icon-offset); }
	.toast--button { cursor: pointer; justify-content: center; text-align: center; }
	.toast--button .toast-line { flex: 0 1 auto; }
	.toast--button:focus-visible, .toast--compact:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
	.toast--pinned.toast--compact { min-width: 0; align-self: center; cursor: pointer; font-size: var(--text-footnote); }
	.toast-line { min-width: 0; flex: 1; }
	.toast-detail { display: block; color: var(--text-secondary); font-weight: 400; }
	.toast--error .toast-text, .toast--error .toast-action { color: var(--red-text); }
	.toast--error .toast-text { font-weight: var(--fw-semibold); }
	.toast-time { color: var(--text-tertiary); font-weight: 400; font-variant-numeric: tabular-nums; white-space: nowrap; }
	.toast-time::before { content: '\00a0·\00a0'; }
	.toast--pinned { gap: var(--space-2); min-width: min(26rem, calc(var(--toast-column) - var(--space-7))); }
	.toast--pinned .toast-line { flex: 0 0 auto; white-space: nowrap; }
	.toast--pinned:global([data-fit='no-time']) .toast-time, .toast--pinned:global([data-fit='wrap']) .toast-time { display: none; }
	.toast--pinned:global([data-fit='wrap']) .toast-line { flex: 1 1 auto; white-space: normal; }
	.toast--pinned .toast-close { margin-left: auto; }
	.toast-action { display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; min-height: var(--space-7); background: none; border: none; border-radius: var(--radius-xs, 4px); color: inherit; cursor: pointer; font: inherit; font-weight: 700; padding: 0 var(--space-1); text-decoration: underline; text-underline-offset: var(--toast-action-underline-offset); }
	.toast-action:hover, .toast-action:focus-visible { background: var(--bg-hover); }
	.toast-close { background: none; border: none; cursor: pointer; color: inherit; opacity: 0.75; font-size: var(--text-callout); line-height: 1; padding: 0 var(--toast-close-inline-padding); width: var(--space-7); height: var(--space-7); flex-shrink: 0; }
	.toast-close:hover { opacity: 1; }
	@media (max-width: 37.5rem) { .toast--pinned { align-self: stretch; max-width: none; padding-right: var(--space-1); } .toast--pinned.toast--compact { align-self: center; padding-right: var(--space-4); } .toast-action { min-height: var(--touch-target); } .toast-close { width: var(--touch-target); height: var(--touch-target); } }
	@media (prefers-reduced-motion: reduce) { .toast { animation: none; } }
	@keyframes toast-in { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: translateY(0); } }
</style>
