<script lang="ts">
	import type { OperatorArtifactLink } from '$lib/api/plans';

	let {
		artifact,
		nodeTitle,
		onOpen,
	}: {
		artifact: OperatorArtifactLink;
		nodeTitle: string;
		onOpen: (artifact: OperatorArtifactLink, nodeTitle: string) => void;
	} = $props();
</script>

<button
	class="ask-open"
	type="button"
	aria-label={`Open the review item for ${nodeTitle}`}
	onclick={(event) => {
		event.stopPropagation();
		onOpen(artifact, nodeTitle);
	}}
>Open</button>

<style>
	.ask-open {
		flex: none; border: 0; border-radius: var(--radius-xs); background: transparent;
		color: var(--text-tertiary); padding: var(--space-1) 2px var(--space-1) var(--space-2);
		font: inherit; font-size: var(--text-caption); cursor: pointer;
	}
	/* Hover colour is for a pointer: a tap on touch leaves :hover stuck on the last thing tapped. */
	@media (hover: hover) {
		.ask-open:hover { color: var(--text-primary); background: var(--bg-hover); }
	}
	.ask-open:focus-visible { outline: 2px solid var(--violet); outline-offset: 2px; }
	@media (max-width: 620px) { .ask-open { min-height: 44px; } }
</style>
