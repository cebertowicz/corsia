<script lang="ts">
	import { Avatar, Button } from '$lib/components/ui';
	import MiddleName from '../MiddleName.svelte';
	import { agentColor } from '$lib/docPresence';
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
		{#if step}<span class="task-who">{#if !menu}<Avatar name={step.owner_name} color={agentColor(step.owner_thread_id)} hidden />{/if}<span class="task-name"><strong><MiddleName name={step.owner_name} /></strong>{#if !menu}:{/if}</span></span>{/if}
		{#if summary}<span class="task-summary">{summary}</span>{/if}
		<Button size="sm" variant="ghost" loading={busy} onclick={dismiss}>{DISMISS_ALL}</Button>
		{#if failure}<span role="status">{failure}</span>{/if}
	</div>
{/if}

<style>
	/* The asker leads on its own row, as the card it replaced showed it: the avatar and name, then the summary. */
	.task-dismiss { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-1); width: 100%; min-width: 0; }
	.task-who { display: flex; flex: 0 0 100%; align-items: center; gap: var(--space-2); min-width: 0; font-size: var(--text-footnote); color: var(--text-primary); }
	/* The colon sits against the name, with no row gap between them. */
	.task-name { display: flex; min-width: 0; }
	.task-name strong { min-width: 0; font-weight: var(--fw-semibold); }
	.task-summary { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.task-dismiss :global(.btn) { min-width: var(--touch-target); min-height: var(--touch-target); }
	/* Beside the summary the button keeps its look; wrapped onto its own row its label starts under the summary's first letter: the pull-in by the button's side padding is paid back in the row's column gap, which only an item that shares a row receives. */
	.task-dismiss:not(.menu) { column-gap: calc(var(--space-1) + var(--button-pad-x-sm)); }
	.task-dismiss:not(.menu) :global(.btn) { margin-inline-start: calc(-1 * var(--button-pad-x-sm)); }
	.task-dismiss.menu { display: grid; }
	.task-dismiss.menu :global(.btn) { width: 100%; justify-content: flex-start; }
</style>
