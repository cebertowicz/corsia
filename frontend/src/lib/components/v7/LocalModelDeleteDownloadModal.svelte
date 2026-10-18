<script lang="ts">
	import { get } from 'svelte/store';
	import { deleteLocalModelDownload } from '$lib/conductorSources';
	import { token } from '$lib/stores/auth';
	import type { ProviderModel } from '$lib/stores/providerModels';
	import { Button, Modal } from '$lib/components/ui';

	interface Props {
		model: ProviderModel;
		onClose: () => void;
		onDeleted: () => Promise<void>;
	}

	const { model, onClose, onDeleted }: Props = $props();
	let deleting = $state(false);
	let error = $state('');
	const size = $derived(model.sizeBytes ? `${(model.sizeBytes / (1000 ** 3)).toFixed(1)} GB` : null);

	async function deleteDownload() {
		const t = get(token);
		if (!t || deleting) {
			if (!t) error = "Couldn't delete this model download.";
			return;
		}
		deleting = true;
		error = '';
		try {
			await deleteLocalModelDownload(t, model.id);
			await onDeleted();
			onClose();
		} catch (caught) {
			error = caught instanceof Error ? caught.message : "Couldn't delete this model download.";
		} finally {
			deleting = false;
		}
	}
</script>

<Modal
	title={`Delete ${model.name}?`}
	size="sm"
	role="alertdialog"
	onClose={() => { if (!deleting) onClose(); }}
>
	<p class="copy">
		Delete{size ? ` the ${size} download` : ' the download'} of <strong>{model.name}</strong> from this computer? You can download it again later.
	</p>
	{#if error}<p class="error">{error}</p>{/if}
	{#snippet foot()}
		<Button variant="ghost" disabled={deleting} onclick={onClose}>Cancel</Button>
		<Button variant="danger" loading={deleting} onclick={deleteDownload}>Delete download</Button>
	{/snippet}
</Modal>

<style>
	.copy { margin: 0; color: var(--text-secondary); line-height: 1.5; }
	.copy strong { color: var(--text-primary); }
	.error { margin: var(--space-3) 0 0; color: var(--red); }
</style>
