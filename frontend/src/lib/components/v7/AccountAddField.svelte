<script lang="ts">
	import { keepTyped } from '$lib/actions/keepTyped';
	import { Button, Field } from '$lib/components/ui';

	/** The "Account name" field that adds a provider account: Enter adds, Escape cancels. */
	let { providerLabel, busy, error, name = $bindable(''), onAdd, onCancel }: {
		providerLabel: string;
		busy: boolean;
		error: string;
		name?: string;
		onAdd: () => void;
		onCancel: () => void;
	} = $props();
</script>

<Field label="Account name" {error}>
	{#snippet trailing()}
		<div class="inline-actions">
			<Button size="sm" variant="primary" loading={busy} disabled={!name.trim()} onclick={onAdd}>Add</Button>
			<Button size="sm" disabled={busy} onclick={() => { if (!busy) onCancel(); }}>Cancel</Button>
		</div>
	{/snippet}
	<input use:keepTyped={{ prose: false }} aria-label={`Name for new ${providerLabel} account`} readonly={busy} bind:value={name} onkeydown={(event) => {
		if (event.key === 'Enter' || event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); }
		if (event.key === 'Enter') onAdd();
		if (event.key === 'Escape' && !busy) onCancel();
	}} />
</Field>
