<script lang="ts">
	import { Button } from '$lib/components/ui';
	import type { ActivityQueueItem } from '$lib/api/activityQueue';
	import MissingDraft from './MissingDraft.svelte';

	let { item, offline, onActed, onOpenChat }: {
		item: ActivityQueueItem;
		offline: boolean;
		onActed: () => Promise<void> | void;
		onOpenChat: (threadId: string) => void;
	} = $props();
	const missing = $derived(item.kind === 'draft' && 'missing' in item.detail && item.detail.missing);
	const artifact = $derived(missing && 'artifact_id' in item.detail ? item.detail.artifact_id : null);
	const thread = $derived(item.source.thread_id);
	const label = $derived(item.source.thread_label === 'Untitled thread' || item.source.thread_label.length > 24
		? 'Open the chat' : `Open ${item.source.thread_label}’s chat`);
</script>

<h1 class="dh">{item.title}</h1>
{#snippet chatButton()}
	{#if thread}
		<span class="chat-action"><Button size="sm" variant="secondary" onclick={() => onOpenChat(thread)}><span class="chat-label">{label}</span></Button></span>
	{/if}
{/snippet}
{#if missing}
	<MissingDraft artifactId={artifact} draftId={item.external_id} unconfirmed={'unconfirmed' in item.detail && item.detail.unconfirmed === true} {offline} {onActed}>{@render chatButton()}</MissingDraft>
{:else}
	{#if item.kind === 'draft'}<p class="dctx" role="status">Couldn’t open this draft from Activity. Open its chat to find the file.</p>{/if}
	{@render chatButton()}
{/if}

<style>
	.chat-action { display: inline-flex; min-width: 0; max-width: 100%; }
	.chat-action :global(button) { min-width: 0; max-width: 100%; }
	.chat-label { overflow: hidden; text-overflow: ellipsis; }
	.dh { margin: 0 0 var(--space-3); font-size: var(--text-title3); line-height: 1.35; color: var(--text-primary); }
	.dctx { font-size: var(--text-body); line-height: var(--lh-relaxed); margin: 0 0 var(--space-3); color: var(--text-secondary); }
	:global(.activity.phone) .dctx { font-size: var(--text-input); }
</style>
