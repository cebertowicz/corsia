<script lang="ts">
	import type { PresenceView } from './DocPresenceStatus.svelte';
	import AvatarStack from './ui/AvatarStack.svelte';

	let { view, artifactId, instanceId, ready }: { view: PresenceView | null; artifactId: string | null; instanceId?: string; ready: boolean } = $props();
</script>

{#if ready && artifactId && view?.artifactId === artifactId && view.instanceId === instanceId && view.participants.length}
	<span class="presence-row" aria-label="Recent document activity"><AvatarStack people={view.participants} max={3} /></span>
{/if}
