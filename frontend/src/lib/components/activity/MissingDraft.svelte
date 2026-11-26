<script lang="ts">
	import { discardEmailDraft } from '$lib/api/emailDrafts';
	import { apiFetch } from '$lib/api/request';
	import { Button } from '$lib/components/ui';
	import type { Snippet } from 'svelte';

	let { artifactId, draftId, unconfirmed, offline, onActed, children }: { artifactId: string | null; draftId: string; unconfirmed: boolean; offline: boolean; onActed: () => Promise<void> | void; children?: Snippet } = $props();
	let sending = $state(false);
	let failure = $state(false);
	async function acknowledge() {
		if (sending) return;
		sending = true; failure = false;
		try {
			const response = await apiFetch(`/api/attention/drafts/${encodeURIComponent(draftId)}/acknowledge`, { method: 'POST' });
			if (response.status !== 204) throw new Error('Acknowledgement failed');
			await onActed();
		} catch { failure = true; }
		finally { sending = false; }
	}
	async function discard() {
		if (!artifactId || sending) return;
		sending = true;
		failure = false;
		try {
			const result = await discardEmailDraft(artifactId);
			if (result.state === 'discarded') await onActed();
			else failure = true;
		} catch { failure = true; }
		finally { sending = false; }
	}
</script>

{#if unconfirmed}
	<p role="status">Couldn’t confirm the email left. Check your Sent mail. If it isn’t there, ask the agent for a new draft.</p>
{:else}
	<p role="status">This draft’s file is missing, so it can’t be sent.</p>
{/if}
<div class="actions">
	{#if unconfirmed}<Button size="sm" variant="primary" loading={sending} disabled={offline} onclick={() => void acknowledge()}>Done</Button>
	{:else if artifactId}<Button size="sm" variant="secondary" loading={sending} disabled={offline} onclick={() => void discard()}>Don’t send</Button>{/if}
	{@render children?.()}
</div>
{#if offline}<p class="act-failure-row offline-notice" role="status">{unconfirmed ? 'Done is paused until this reconnects.' : 'Don’t send is paused until this reconnects.'}</p>{/if}
{#if failure}<p class="failure" role="alert">{unconfirmed ? 'Couldn’t mark this done. Try again.' : 'Don’t send didn’t go through. Try again.'}</p>{/if}

<style>
	.actions { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-bottom: var(--space-3); }
	p { margin: 0 0 var(--space-3); color: var(--text-secondary); font-size: var(--text-body); line-height: var(--lh-relaxed); }
	.offline-notice { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	.failure { margin: 0; color: var(--red-text); font-size: var(--text-footnote); }
	:global(.activity.phone) p:not(.offline-notice):not(.failure) { font-size: var(--text-input); }
</style>
