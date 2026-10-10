<script lang="ts">
	import { untrack } from 'svelte';
	import { Button, Field, Message, Section } from '$lib/components/ui';
	import { setModelLabel } from '$lib/stores/harnessProviders';

	interface Props {
		dbId: string;
		/** The saved label, if any. */
		label?: string | null;
		/** The name discovery reported, shown when no label is saved. */
		discoveredName: string;
		onSaved?: () => void;
	}

	const { dbId, label = null, discoveredName, onSaved }: Props = $props();

	// The draft starts from the saved label and is then the user's to edit.
	let draft = $state(untrack(() => label ?? ''));
	let saving = $state(false);
	let errMsg = $state<string | null>(null);

	const dirty = $derived(draft.trim() !== (label ?? ''));

	async function save() {
		saving = true;
		errMsg = null;
		const r = await setModelLabel(dbId, draft);
		saving = false;
		if (!r.ok) {
			errMsg = r.message ?? 'Could not save the label.';
			return;
		}
		draft = draft.trim();
		onSaved?.();
	}
</script>

<Section title="Name">
	<Field
		label="Label in the model picker"
		hint={`Leave empty to use the name your CLI reports: ${discoveredName}.`}
	>
		<input placeholder={discoveredName} bind:value={draft} />
	</Field>
	{#if errMsg}<Message variant="err">{errMsg}</Message>{/if}
	<div class="actions">
		<Button variant="primary" onclick={save} disabled={!dirty || saving} loading={saving}>
			{saving ? 'Saving…' : 'Save'}
		</Button>
	</div>
</Section>

<style>
	.actions { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
