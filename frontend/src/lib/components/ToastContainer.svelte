<script lang="ts">
	import { toasts } from '$lib/stores/toast';
	import { toastClearance } from '$lib/stores/toastClearance';

	let toastHeight = $state(0);

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
</script>

{#if $toasts.length > 0}
	<div
		class="toast-container"
		aria-live="polite"
		aria-atomic="false"
		style:--toast-clearance="{$toastClearance}px"
		style:--toast-height="{toastHeight}px"
		use:measureToastHeight
	>
		{#each $toasts as toast (toast.id)}
			<div
				class="toast toast--{toast.type ?? 'info'}"
				role="status"
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
				<span class="toast-text">{toast.message}</span>
				{#if toast.action}
					<button class="toast-action" onclick={() => void toasts.runAction(toast.id)}>
						{toast.action.label}
					</button>
				{/if}
				<button class="toast-close" onclick={() => toasts.dismiss(toast.id)} aria-label="Dismiss">×</button>
			</div>
		{/each}
	</div>
{/if}

<style>
	.toast-container {
		--toast-base-offset: var(--space-6);
		position: fixed;
		bottom: min(
			max(var(--toast-base-offset), calc(var(--toast-clearance) + var(--space-4))),
			calc(100dvh - var(--toast-height) - var(--header-height) - var(--space-3) - env(safe-area-inset-top, 0px))
		);
		left: 50%;
		transform: translateX(-50%);
		width: max-content;
		max-width: calc(100vw - var(--space-7));
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		z-index: 9999;
		pointer-events: none;
		transition: bottom var(--dur-drawer) var(--ease-ios-sheet);
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

	.toast--info {
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

	.toast-action {
		background: none;
		border: none;
		border-radius: var(--radius-xs, 4px);
		color: inherit;
		cursor: pointer;
		font: inherit;
		font-weight: 700;
		padding: 2px 4px;
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
	.toast-text { min-width: 0; flex: 1; }
	@media (max-width: 600px) {
		.toast-container { --toast-base-offset: calc(var(--space-4) + env(safe-area-inset-bottom, 0px)); }
		.toast-close { width: 44px; height: 44px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.toast-container { transition: none; }
	}

	@keyframes toast-in {
		from { opacity: 0; transform: translateY(8px); }
		to { opacity: 1; transform: translateY(0); }
	}
</style>
