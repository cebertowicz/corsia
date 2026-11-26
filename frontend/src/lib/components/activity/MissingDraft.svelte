<script lang="ts">
	import { discardEmailDraft } from '$lib/api/emailDrafts';
	import { Button } from '$lib/components/ui';

	let { artifactId, offline, onActed }: { artifactId: string | null; offline: boolean; onActed: () => Promise<void> | void } = $props();
	let sending = $state(false);
	let failure = $state(false);
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

<p role="status">This draft’s file no longer exists, so it can’t be sent.</p>
{#if artifactId}<Button size="sm" variant="secondary" loading={sending} disabled={offline} onclick={() => void discard()}>Don’t send</Button>{/if}
{#if failure}<p role="alert">Couldn’t remove this draft. Try again.</p>{/if}

<style>
	p { margin: 0 0 var(--space-3); color: var(--text-secondary); font-size: var(--text-body); line-height: var(--lh-relaxed); }
	:global(.activity.phone) p { font-size: var(--text-input); }
</style>
