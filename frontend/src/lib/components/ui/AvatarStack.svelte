<script lang="ts">
	import Avatar from './Avatar.svelte';
	import Popover from './Popover.svelte';

	export type AvatarParticipant = {
		id?: string;
		name: string;
		color: string;
		detail?: string;
		active?: boolean;
	};

	let { people, max = 3 }: { people: AvatarParticipant[]; max?: number } = $props();
	const visible = $derived(people.slice(0, max));
	const remaining = $derived(Math.max(0, people.length - visible.length));
	const remainingNames = $derived(people.slice(max).map((person) => person.name).join(', '));
	let openId = $state<string | null>(null);
	let anchors = $state<Record<string, HTMLButtonElement>>({});

	const participantId = (person: AvatarParticipant) => person.id ?? person.name;
	const descriptionId = (person: AvatarParticipant) =>
		`avatar-detail-${encodeURIComponent(participantId(person)).replaceAll('%', '-')}`;
	function open(person: AvatarParticipant) { openId = participantId(person); }
	function close() { openId = null; }
	function closeWhenFocusLeaves(event: FocusEvent) {
		const stack = event.currentTarget;
		const next = event.relatedTarget;
		if (!(stack instanceof HTMLElement)) return;
		if (!(next instanceof Node) || !stack.contains(next)) close();
	}
</script>

<div class="stack" role="group" aria-label={`${people.length} participants`} onfocusout={closeWhenFocusLeaves}>
	{#each visible as person (participantId(person))}
		{#if person.detail}
			<button
				bind:this={anchors[participantId(person)]}
				type="button"
				class="participant-trigger"
				class:arrival={person.active}
				style={`--participant-color:${person.color}`}
				data-participant-id={participantId(person)}
				aria-label={person.name}
				aria-describedby={descriptionId(person)}
				onmouseenter={() => open(person)}
				onmouseleave={close}
				onfocus={() => open(person)}
				onclick={() => openId === participantId(person) ? close() : open(person)}
			>
				<Avatar name={person.name} color={person.color} hidden />
				<span id={descriptionId(person)} class="sr-only">{person.detail}</span>
			</button>
			{#if openId === participantId(person)}
				<Popover anchor={anchors[participantId(person)]} open onClose={close} placement="bottom">
					{#snippet children()}<p class="detail-copy"><span class="detail-name">{person.name}</span> · {person.detail}</p>{/snippet}
				</Popover>
			{/if}
		{:else}
			<span class="participant-slot"><Avatar name={person.name} color={person.color} /></span>
		{/if}
	{/each}
	{#if remaining}<span class="more" title={`and ${remaining} more: ${remainingNames}`}>+{remaining}<span class="sr-only">and {remaining} more: {remainingNames}</span></span>{/if}
</div>

<style>
	.stack { display: inline-flex; align-items: center; }
	.stack > .participant-trigger, .stack > .participant-slot, .stack > .more { margin-left: -5px; }
	.stack > :first-child { margin-left: 0; }
	.stack :global(.avatar) { border: 2px solid var(--bg-primary); }
	.participant-slot { display: inline-flex; }
	.participant-trigger { display: inline-flex; margin: 0; padding: 0; border: 0; border-radius: var(--radius-circle); background: none; cursor: pointer; }
	.participant-trigger:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.participant-trigger.arrival { box-shadow: 0 0 0 2px color-mix(in srgb, var(--participant-color) 45%, transparent); animation: participant-arrival 700ms ease-out infinite alternate; }
	@keyframes participant-arrival { from { box-shadow: 0 0 0 1px color-mix(in srgb, var(--participant-color) 35%, transparent); } to { box-shadow: 0 0 0 4px color-mix(in srgb, var(--participant-color) 12%, transparent); } }
	.more { margin-left: -5px; padding: 1px 5px; border: 2px solid var(--bg-primary); border-radius: var(--radius-pill); background: var(--bg-tertiary); color: var(--text-secondary); font-size: var(--text-caption); font-weight: var(--fw-semibold); }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
	.detail-copy { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	.detail-name { color: var(--text-primary); font-weight: var(--fw-semibold); }
	@media (prefers-reduced-motion: reduce) { .participant-trigger.arrival { animation: none; } }
	@media (max-width: 768px) {
		.participant-trigger, .participant-slot { min-width: 40px; min-height: 40px; align-items: center; justify-content: center; margin-left: -9px; }
		.participant-trigger:first-child, .participant-slot:first-child { margin-left: 0; }
		.stack :global(.avatar) { margin-left: 0; }
	}
</style>
