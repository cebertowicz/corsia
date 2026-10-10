<script lang="ts">
	import { Button, Field, Message, Section } from '$lib/components/ui';
	import type { ImageInput } from '$lib/stores/providerModels';
	import { setModelImageInput } from '$lib/stores/networkServers';

	interface Props {
		serverId: string;
		modelId: string;
		imageInput: ImageInput | null | undefined;
		onSaved?: () => void;
	}

	const { serverId, modelId, imageInput, onSaved }: Props = $props();

	type Mode = 'auto' | 'yes' | 'no';
	const current = $derived<Mode>(imageInput?.source === 'user' ? (imageInput.result as Mode) : 'auto');
	let draft = $state<Mode | null>(null);
	const choice = $derived<Mode>(draft ?? current);
	let saving = $state(false);
	let errMsg = $state<string | null>(null);

	const status = $derived.by(() => {
		if (!imageInput) return 'Not checked yet. CORSIA checks when the model is discovered or the endpoint is tested.';
		const answer = imageInput.result === 'yes' ? 'Accepts images.'
			: imageInput.result === 'no' ? 'Text only.'
			: 'Could not be determined.';
		return `${answer} ${imageInput.detail}`;
	});

	async function save() {
		saving = true;
		errMsg = null;
		const r = await setModelImageInput(serverId, modelId, choice);
		saving = false;
		if (!r.ok) {
			errMsg = r.message ?? 'Could not save image support.';
			return;
		}
		draft = null;
		onSaved?.();
	}
</script>

<Section title="Image input">
	<p class="hint" data-testid="image-input-status">{status}</p>
	<Field label="Image support" hint="Detect sends the model one small test image and asks for its colour.">
		<select
			value={choice}
			onchange={(e) => (draft = (e.currentTarget as HTMLSelectElement).value as Mode)}
			aria-label="Image support"
		>
			<option value="auto">Detect automatically</option>
			<option value="yes">Accepts images</option>
			<option value="no">Text only</option>
		</select>
	</Field>
	{#if errMsg}<Message variant="err">{errMsg}</Message>{/if}
	<div class="actions">
		<Button variant="primary" onclick={save} disabled={saving || (choice === current && choice !== 'auto')} loading={saving}>
			{saving ? (choice === 'auto' ? 'Detecting…' : 'Saving…') : (choice === 'auto' ? 'Detect now' : 'Save')}
		</Button>
	</div>
</Section>

<style>
	.hint {
		font-size: var(--text-footnote); color: var(--text-tertiary); line-height: 1.5;
		margin: 0 0 10px;
	}
	.actions { display: flex; justify-content: flex-end; margin-top: 12px; }
</style>
