<script lang="ts">
	/**
	 * The inline confirm under a Settings row: the question, and what the action does. The row's own actions carry the
	 * Confirm and Cancel buttons (Settings › System's Disconnect, Turn off and Remove of Urgent notifications).
	 */
	let { question, detail = '' }: { question: string; detail?: string } = $props();
</script>

<div class="confirm-block" role="group" aria-label={question}>
	<span class="confirm-text">{question}</span>
	{#if detail}<span class="confirm-detail">{detail}</span>{/if}
</div>

<style>
	.confirm-text { font-size: var(--text-caption); color: var(--text-secondary); }
	.confirm-block { display: flex; flex-direction: column; gap: var(--space-1); padding: var(--space-1) var(--space-2); overflow-wrap: anywhere; }
	.confirm-detail { font-size: var(--text-caption); color: var(--text-tertiary); }
</style>
