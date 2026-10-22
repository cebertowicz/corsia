<script lang="ts">
	// The model list's Local only notes (design §5): `empty` when no local model is ready, `quiet` once local rows are showing.
	// onsetuplocal is passed only when nothing else in the sheet offers to add a local model.
	let { kind, projectCloudOff, inherited = false, onsetuplocal, onprojectsettings }: {
		kind: 'empty' | 'quiet'; projectCloudOff: boolean; inherited?: boolean; onsetuplocal?: () => void; onprojectsettings: () => void;
	} = $props();
</script>

{#if kind === 'empty'}
	<div class="model-list-empty">
		{#if projectCloudOff}
			This project uses local models only, and no local model is ready.<br /><button type="button" class="add-local-link" onclick={onprojectsettings}>Open project settings</button>
		{:else}
			No local model is ready.{#if onsetuplocal}<br /><button type="button" class="add-local-link" onclick={onsetuplocal}>Set up a local model</button>{/if}
		{/if}
	</div>
{:else if projectCloudOff}
	<div class="model-list-empty">This project uses local models only.</div>
{:else if inherited}
	<div class="model-list-empty">This chat and its agents use local models only.</div>
{/if}

<style>
	.add-local-link { padding: var(--space-2) var(--space-3); color: var(--accent-text); background: transparent; border: 0; font: inherit; cursor: pointer; }
</style>
