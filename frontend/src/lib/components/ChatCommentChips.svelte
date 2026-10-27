<script lang="ts">
	import type { ChatComment } from '$lib/chatCommentAnchors';
	let { comments, onEdit, onRemove }: { comments: ChatComment[]; onEdit: (id: string) => void; onRemove: (id: string) => void } = $props();
	function onKey(event: KeyboardEvent, id: string) {
		if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onEdit(id); }
	}
</script>

<div class="comment-chip-row" aria-label="Pending comments">
	{#each comments as comment (comment.id)}
		<div class="attachment-chip comment-chip" role="button" tabindex="0" aria-label={`Edit comment: ${comment.quote}`} onclick={() => onEdit(comment.id)} onkeydown={event => onKey(event, comment.id)}>
			<div class="attachment-icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg></div>
			<span class="attachment-name">{comment.quote}</span>
			<button class="attachment-remove" aria-label="Remove comment" onclick={event => { event.stopPropagation(); onRemove(comment.id); }}><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 6L6 18M6 6l12 12" /></svg></button>
		</div>
	{/each}
</div>

<style>
	.comment-chip-row { display: flex; flex: 0 0 100%; gap: 8px; min-width: 0; overflow-x: auto; overflow-y: hidden; scrollbar-width: thin; }
	.comment-chip { flex: 0 0 auto; cursor: pointer; }
	.comment-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
