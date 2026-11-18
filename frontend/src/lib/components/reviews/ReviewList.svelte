<script lang="ts">
	import { tick } from 'svelte';
	import { reviewContinuationText, reviewStatus } from '$lib/reviewCopy';
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import { agentColor } from '$lib/docPresence';
	import type { DocumentReviewState } from '$lib/docReviewState.svelte';
	let { model }: { model: DocumentReviewState } = $props();
	/**
	 * Cancel keeps the focus through its request: while the request is pending the button refuses presses instead of
	 * being disabled, and once the row goes the focus moves to the next Cancel, else the one before, else back into the
	 * surface (its search field when there is one, else the popover card or the drawer panel), so Escape still reaches the
	 * surface and never the page beneath it. Positions count Cancel buttons only, since rows that can't be cancelled have none.
	 */
	async function cancel(event: MouseEvent, id: string) {
		if (model.cancelPending.includes(id)) return;
		const button = event.currentTarget as HTMLElement;
		const surface = button.closest<HTMLElement>('[data-review-surface]');
		const index = model.requests.filter((request) => request.may_cancel).findIndex((request) => request.id === id);
		await model.cancel(id);
		await tick();
		const active = document.activeElement;
		if (button.isConnected || !surface?.isConnected || (active && active !== document.body && active.isConnected)) return;
		const cancels = [...surface.querySelectorAll<HTMLElement>('[data-review-request] .cancel')];
		const next = cancels[index] ?? cancels[index - 1] ?? surface.querySelector<HTMLElement>('[data-review-autofocus]') ?? surface.closest<HTMLElement>('[data-popover], [data-adaptive-drawer] [role="dialog"]');
		next?.focus({ preventScroll: true });
	}
</script>

{#if model.requests.length}
	<ul class="requests" aria-label="Review requests">
		{#each model.requests as request (request.id)}
			<li data-review-request>
				<Avatar name={request.reviewer_name} color={agentColor(request.reviewer_thread_id)} hidden size="md" />
				<span class="who">
					<span class="name">{request.reviewer_name}</span>
					<span class="status" title={request.outcome === 'open' && request.state === 'failed' ? reviewContinuationText(request.reviewer_name, request.reason) : undefined}>{reviewStatus(request.outcome, request.state, request.reviewer_name, request.created_at, request.reason)}</span>
					{#if model.cancelErrors[request.id]}<span class="error" role="alert">{model.cancelErrors[request.id]}</span>{/if}
				</span>
				{#if request.may_cancel}
					<button type="button" class="cancel" aria-disabled={model.cancelPending.includes(request.id)} aria-label={`Cancel request to ${request.reviewer_name}`} onclick={(event) => cancel(event, request.id)}>
						{model.cancelPending.includes(request.id) ? 'Cancelling…' : model.cancelErrors[request.id] ? 'Retry' : 'Cancel'}
					</button>
				{/if}
			</li>
		{/each}
	</ul>
{/if}

<style>
	.requests { list-style: none; margin: 0; padding: 6px 0 0; border-top: 1px solid var(--border-subtle); display: flex; flex-direction: column; max-height: 180px; overflow: auto; font-family: var(--font-sans); font-size: var(--text-subhead); }
	li { display: flex; align-items: center; gap: 8px; min-height: 36px; padding: 2px 0; }
	.who { display: flex; flex-direction: column; flex: 1; min-width: 0; }
	.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); }
	.status { color: var(--text-secondary); font-size: var(--text-footnote); }
	.error { color: var(--red); font-size: var(--text-footnote); }
	.cancel { flex: none; padding: 2px 6px; border: 0; border-radius: var(--radius-sm); background: none; color: var(--text-secondary); font: inherit; font-size: var(--text-footnote); cursor: pointer; }
	.cancel:hover:not([aria-disabled="true"]) { color: var(--text-primary); background: var(--bg-hover); }
	.cancel:focus-visible { outline: 2px solid var(--accent); }
	.cancel[aria-disabled="true"] { cursor: default; opacity: 0.6; }
	@media (max-width: 768px) { li { min-height: 44px; } .cancel { min-height: 44px; padding: 0 10px; } }
</style>
