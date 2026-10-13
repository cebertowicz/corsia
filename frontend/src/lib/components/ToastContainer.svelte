<script lang="ts">
	import { toasts, type Toast } from '$lib/stores/toast';
	import { toastAnchor } from '$lib/stores/toastAnchor';
	import { toastClearance } from '$lib/stores/toastClearance';
	import { toastFit } from '$lib/stores/toastFit';
	import { flip } from 'svelte/animate';
	import { cubicOut } from 'svelte/easing';

	let toastHeight = $state(0);

	/** The upper toast glides into the freed place when the one below it goes. */
	function flipParams() {
		const reduced =
			typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
		return { duration: reduced ? 0 : 200, easing: cubicOut };
	}

	function measureToastHeight(node: HTMLElement) {
		const update = () => {
			toastHeight = node.getBoundingClientRect().height;
		};
		if (typeof ResizeObserver === 'undefined') {
			update();
			return {};
		}
		const observer = new ResizeObserver(update);
		observer.observe(node);
		update();
		return {
			destroy() {
				observer.disconnect();
			},
		};
	}

	/** Only the pinned slot settles its fit; the parameter re-measures on change. */
	function fitParam(toast: Toast): string | null {
		const pinned = toast.pinned;
		if (!pinned) return null;
		return `${toast.message}|${pinned.time ?? ''}|${toast.action?.label ?? ''}`;
	}

	function pinnedFit(node: HTMLElement, param: string | null) {
		if (param === null) return {};
		return toastFit(node, param);
	}
</script>

{#if $toasts.length > 0}
	<div
		class="toast-container"
		aria-live="polite"
		aria-atomic="false"
		style:--toast-clearance="{$toastClearance}px"
		style:--toast-height="{toastHeight}px"
		style:--toast-anchor-x={$toastAnchor ? `${$toastAnchor.x}px` : undefined}
		style:--toast-anchor-width={$toastAnchor ? `${$toastAnchor.width}px` : undefined}
		use:measureToastHeight
	>
		{#each $toasts as toast (toast.id)}
			{@const type = toast.type ?? 'info'}
			{@const pinned = toast.pinned}
			<div
				class="toast toast--{type}{pinned ? ' toast--pinned' : ''}"
				role={pinned ? (type === 'error' ? 'alert' : undefined) : 'status'}
				data-fit="full"
				animate:flip={flipParams()}
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
					<span class="toast-icon toast-spinner" aria-hidden="true"></span>
				{:else if type === 'success'}
					<svg class="toast-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="8 12.5 11 15.5 16 9"/></svg>
				{:else if type === 'info' && pinned}
					<svg class="toast-icon toast-icon--update" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 16V8M8 12l4-4 4 4"/></svg>
				{:else if type === 'error'}
					<svg class="toast-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12.5"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
				{/if}
				<span class="toast-line">
					<span class="toast-text" role={pinned && type !== 'error' ? 'status' : undefined}>{toast.message}</span>
					{#if pinned?.time}
						<span class="toast-time" aria-hidden="true">{pinned.time}</span>
					{/if}
				</span>
				{#if toast.action}
					<button class="toast-action" onclick={() => void toasts.runAction(toast.id)}>
						{toast.action.label}
					</button>
				{/if}
				<button
					class="toast-close"
					onclick={() => toasts.dismiss(toast.id)}
					aria-label={pinned?.closeLabel ?? 'Dismiss'}
					title={pinned?.closeLabel}>×</button>
			</div>
		{/each}
	</div>
{/if}

<style>
	.toast-container {
		--toast-base-offset: var(--space-6);
		/* The column the stack centres on: the chat column in a thread, the main
		   area elsewhere, the window until one registers. */
		--toast-column: min(100vw, var(--toast-anchor-width, 100vw));
		position: fixed;
		bottom: min(
			max(var(--toast-base-offset), calc(var(--toast-clearance) + var(--space-4))),
			calc(100dvh - var(--toast-height) - var(--header-height) - var(--space-3) - env(safe-area-inset-top, 0px))
		);
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
			bottom var(--dur-drawer) var(--ease-ios-sheet),
			left var(--dur-drawer) var(--ease-ios-sheet);
	}

	.toast {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 16px;
		border-radius: var(--radius-sm);
		font-size: var(--text-subhead);
		font-weight: 500;
		max-width: calc(100vw - 32px);
		box-sizing: border-box;
		white-space: normal;
		overflow-wrap: anywhere;
		pointer-events: all;
		box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
		animation: toast-in var(--dur-standard) ease;
	}

	.toast--info,
	.toast--progress {
		background: var(--bg-secondary);
		color: var(--text-primary);
		border: 1px solid var(--border-subtle);
	}

	.toast--success {
		background: color-mix(in srgb, #22c55e 15%, var(--bg-secondary));
		color: #22c55e;
		border: 1px solid color-mix(in srgb, #22c55e 30%, transparent);
	}

	.toast--error {
		background: color-mix(in srgb, var(--status-blocked) 15%, var(--bg-secondary));
		color: var(--red-text);
		border: 1px solid color-mix(in srgb, var(--status-blocked) 30%, transparent);
	}

	.toast-icon {
		flex-shrink: 0;
	}

	.toast-icon--update {
		color: var(--violet-text, var(--violet));
	}

	.toast-spinner {
		width: 14px;
		height: 14px;
		box-sizing: border-box;
		border: 2px solid color-mix(in srgb, var(--accent) 25%, transparent);
		border-top-color: var(--accent);
		border-radius: 50%;
		animation: toast-spin 0.8s linear infinite;
	}

	.toast-line {
		min-width: 0;
		flex: 1;
	}

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
		opacity: 0.6;
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
			--toast-base-offset: calc(var(--space-4) + env(safe-area-inset-bottom, 0px));
			/* The pinned line spans the column, sharing the composer's edges;
			   ordinary toasts keep their own width. */
			width: calc(var(--toast-column) - var(--space-3) * 2);
			max-width: none;
		}
		.toast--pinned {
			align-self: stretch;
			max-width: none;
			/* The 44px close button is the trailing edge; the line keeps its room. */
			padding-right: 0;
		}
		.toast-action { min-height: 44px; }
		.toast-close { width: 44px; height: 44px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.toast-container { transition: none; }
		.toast-spinner { animation-duration: 2.4s; }
	}

	@keyframes toast-in {
		from { opacity: 0; transform: translateY(8px); }
		to { opacity: 1; transform: translateY(0); }
	}

	@keyframes toast-spin {
		to { transform: rotate(360deg); }
	}
</style>
