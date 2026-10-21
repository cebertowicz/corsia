<script lang="ts">
	import { reviewContinuationText } from '$lib/reviewCopy';
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import { agentColor } from '$lib/docPresence';
	import type { DocumentReviewState } from '$lib/docReviewState.svelte';
	let { model }: { model: DocumentReviewState } = $props();
	const timeLabel = (value: string) => new Date(value).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
</script>

{#if model.requests.length}
	<ul class="requests" aria-label="Review requests">
		{#each model.requests as request (request.id)}
			<li data-review-request>
				<Avatar name={request.reviewer_name} color={agentColor(request.reviewer_thread_id)} hidden size="md" />
				<span class="who">
					<span class="name">{request.reviewer_name}</span>
					<span class="status">{request.outcome === 'failed' ? reviewContinuationText(request.reviewer_name) : request.outcome === 'ended_without_comments' ? 'Ended without comments' : `Requested ${timeLabel(request.created_at)}`}</span>
					{#if model.cancelErrors[request.id]}<span class="error" role="alert">{model.cancelErrors[request.id]}</span>{/if}
				</span>
				{#if request.may_cancel}
					<button type="button" class="cancel" disabled={model.cancelPending.includes(request.id)} aria-label={`Cancel request to ${request.reviewer_name}`} onclick={() => model.cancel(request.id)}>
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
	.cancel:hover:not(:disabled) { color: var(--text-primary); background: var(--bg-hover); }
	.cancel:focus-visible { outline: 2px solid var(--accent); }
	.cancel:disabled { cursor: default; opacity: 0.6; }
	@media (max-width: 768px) { li { min-height: 44px; } .cancel { min-height: 44px; padding: 0 10px; } }
</style>
