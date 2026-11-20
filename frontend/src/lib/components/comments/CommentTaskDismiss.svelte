<script lang="ts">
	import { Button } from '$lib/components/ui';
	import { dismissCommentAttention } from '$lib/api/docs';
	import type { ReviewBarState } from '$lib/reviewBarState.svelte';
	import { DISMISS_ALL, isCommentTask, personStepAsk } from './commentTask';

	let { bar, menu = false }: { bar: ReviewBarState; menu?: boolean } = $props();
	let busy = $state(false);
	let failure = $state('');
	const step = $derived(isCommentTask(bar.read?.person_step) ? bar.read?.person_step : null);
	const attention = $derived(step?.comment_attention);
	const summary = $derived(step ? personStepAsk(step) : '');
	async function dismiss() {
		const id = attention?.artifact_id;
		if (!id || busy) return;
		busy = true;
		failure = '';
		try {
			await dismissCommentAttention(id);
			if (bar.artifactId === id) await bar.load();
		} catch {
			if (bar.artifactId === id) failure = 'Couldn’t dismiss comments. Try again.';
		} finally {
			busy = false;
		}
	}
</script>

{#if attention}
	<div class="task-dismiss" class:menu>
		{#if summary}<span class="task-summary">{summary}</span>{/if}
		<Button size="sm" variant="ghost" loading={busy} onclick={dismiss}>{DISMISS_ALL}</Button>
		{#if failure}<span role="status">{failure}</span>{/if}
	</div>
{/if}

<style>
	.task-dismiss { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-1); width: 100%; min-width: 0; }
	.task-summary { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.task-dismiss :global(.btn) { min-width: var(--touch-target); min-height: var(--touch-target); }
	.task-dismiss.menu { display: grid; }
	.task-dismiss.menu :global(.btn) { width: 100%; justify-content: flex-start; }
</style>
