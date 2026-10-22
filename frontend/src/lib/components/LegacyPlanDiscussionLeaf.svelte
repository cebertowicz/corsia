<script lang="ts">
	import { onMount } from 'svelte';
	import CommentThread from './comments/CommentThread.svelte';
	import Button from './ui/Button.svelte';
	import { docComments, loadComments } from '$lib/stores/docComments';

	let { artifactId, title }: { artifactId: string; title: string } = $props();

	let status = $state<'loading' | 'ready' | 'error'>('loading');
	const comments = $derived($docComments[artifactId] ?? []);

	async function load(): Promise<void> {
		status = 'loading';
		try {
			await loadComments(artifactId);
			status = 'ready';
		} catch {
			status = 'error';
		}
	}

	onMount(() => {
		void load();
	});
</script>

<section class="discussion-archive" aria-label={title}>
	<header>
		<p class="eyebrow">Legacy discussion archive</p>
		<h2>{title}</h2>
		<p class="explanation">Preserved read-only until these existing comments are separately archived or migrated.</p>
	</header>

	{#if status === 'loading'}
		<p class="state" aria-live="polite">Loading discussion…</p>
	{:else if status === 'error'}
		<div class="state error" role="alert">
			<p>This discussion couldn’t load.</p>
			<Button variant="secondary" size="sm" onclick={load}>Retry</Button>
		</div>
	{:else if comments.length === 0}
		<p class="state">No comments are stored in this discussion.</p>
	{:else}
		<div class="comments">
			{#each comments as comment (comment.id)}
				<CommentThread {comment} readOnly />
			{/each}
		</div>
	{/if}
</section>

<style>
	.discussion-archive { display: flex; flex-direction: column; gap: var(--space-4); max-width: 720px; margin: 0 auto; padding: var(--space-4) 0; }
	header { display: flex; flex-direction: column; gap: var(--space-1); }
	.eyebrow { margin: 0; color: var(--text-tertiary); font-size: var(--text-caption); font-weight: var(--fw-semibold); text-transform: uppercase; letter-spacing: var(--ls-cap); }
	h2 { margin: 0; color: var(--text-primary); font-size: var(--text-title3); }
	.explanation, .state { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); line-height: var(--lh-normal); }
	.state { display: flex; min-height: 120px; flex-direction: column; align-items: center; justify-content: center; gap: var(--space-2); text-align: center; }
	.state.error { color: var(--red); }
	.comments { display: flex; flex-direction: column; gap: var(--space-2); }
</style>
