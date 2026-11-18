<script lang="ts">
	import type { ComponentProps } from 'svelte';
	import DocPresenceStatus, { type PresenceParticipant } from './DocPresenceStatus.svelte';
	import AvatarStack from './ui/AvatarStack.svelte';
	import type { PresenceRange } from '$lib/cm/presence-marks';

	let { barTarget, ranges = $bindable<PresenceRange[]>([]), ...status }: ComponentProps<typeof DocPresenceStatus> & { barTarget?: HTMLElement } = $props();
	let participants = $state<PresenceParticipant[]>([]);
	let home = $state<HTMLElement>();
	let view = $state<HTMLElement>();
	// The view follows the bar without taking the document's presence runtime with it.
	$effect(() => {
		const destination = barTarget ?? home;
		if (view && destination) destination.append(view);
	});
</script>

<div hidden bind:this={home}>
	<span bind:this={view} hidden={!barTarget}>
		{#if status.docState === 'ready' && participants.length}
			<span class="presence-row" aria-label="Recent document activity"><AvatarStack people={participants} max={3} /></span>
		{/if}
	</span>
</div>
<DocPresenceStatus {...status} bind:ranges bind:participantsOut={participants} showView={false} />
