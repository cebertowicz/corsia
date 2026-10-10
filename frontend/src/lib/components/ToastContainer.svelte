<script lang="ts">
	import { toasts } from '$lib/stores/toast';
</script>

{#if $toasts.length > 0}
	<div class="toast-container" aria-live="polite" aria-atomic="false">
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
		position: fixed;
		bottom: 24px;
		left: 50%;
		transform: translateX(-50%);
		width: max-content;
		max-width: calc(100vw - 32px);
		display: flex;
		flex-direction: column;
		gap: 8px;
		z-index: 9999;
		pointer-events: none;
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
		.toast-container { bottom: calc(16px + env(safe-area-inset-bottom, 0px)); }
		.toast-close { width: 44px; height: 44px; }
	}

	@keyframes toast-in {
		from { opacity: 0; transform: translateY(8px); }
		to { opacity: 1; transform: translateY(0); }
	}
</style>
