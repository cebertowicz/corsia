<script lang="ts">
	import { onMount } from 'svelte';
	import { Button, Field, Message, Section } from '$lib/components/ui';
	import { loadModelProfile, updateModelProfile } from '$lib/stores/settings';

	interface Props {
		dbId: string;
		/** The provider or per-server window, used when nothing is saved here. */
		providerWindow?: number | null;
		onSaved?: () => void;
	}

	const { dbId, providerWindow = null, onSaved }: Props = $props();

	let saved = $state<number | null>(null);
	let current = $state<number | null>(null);
	let source = $state<string | null>(null);
	let unsaved = $state<number | null>(null);
	let unsavedSource = $state<string | null>(null);
	let draft = $state('');
	let loaded = $state(false);
	let loadFailed = $state(false);
	let saving = $state(false);
	let errMsg = $state<string | null>(null);

	// An empty draft clears the saved window, landing on the unsaved one.
	const parsed = $derived(draft.trim() === '' ? null : Number(draft.trim()));
	const valid = $derived(parsed === null || (Number.isInteger(parsed) && parsed > 0));
	const dirty = $derived(parsed !== saved);
	// Which window is in force is the resolver's answer, never recomputed here.
	const sourceLabel = $derived(
		source === 'saved' ? 'Saved'
			: source === 'default' ? 'CORSIA default'
				: source === 'provider' ? "Provider's window"
					: ''
	);
	// What an empty field does is the resolver's unsaved window, in every state.
	const hint = $derived(
		unsavedSource === 'default' && unsaved !== null
			? `Leave empty for the CORSIA default, ${unsaved.toLocaleString()} tokens.${providerWindow ? ` Provider window: ${providerWindow.toLocaleString()}.` : ''}`
			: providerWindow
				? `CORSIA checkpoints a chat before it outgrows this window. Leave empty to use the provider's ${providerWindow.toLocaleString()} tokens.`
				: 'The provider reports no context window, so set one to use this model. CORSIA checkpoints a chat before it outgrows it.'
	);

	async function refresh() {
		const profile = await loadModelProfile(dbId);
		loadFailed = profile === null;
		if (loadFailed) return;
		saved = profile?.context_window_override ?? null;
		current = profile?.context_window ?? null;
		source = profile?.context_window_source ?? null;
		unsaved = profile?.context_window_unsaved ?? null;
		unsavedSource = profile?.context_window_unsaved_source ?? null;
		draft = saved === null ? '' : String(saved);
	}

	onMount(async () => {
		await refresh();
		loaded = true;
	});

	async function save() {
		if (!valid) return;
		saving = true;
		errMsg = null;
		const ok = await updateModelProfile(dbId, { context_window: parsed });
		saving = false;
		if (!ok) {
			errMsg = 'Could not save the context window.';
			return;
		}
		await refresh();
		onSaved?.();
	}
</script>

<Section title="Checkpointing">
	<p class="hint">
		CORSIA's own setting, never sent to the provider. Sampling and output
		limits stay with the provider.
	</p>
	{#if !loaded}
		<p class="hint">Loading…</p>
	{:else if loadFailed}
		<Message variant="err">Could not load the saved checkpoint window. Close and reopen to retry.</Message>
	{:else}
		{#if current !== null && sourceLabel}
			<p class="hint">
				Current: {current.toLocaleString()} tokens ·
				<span class="source">{sourceLabel}</span>
			</p>
		{/if}
		<Field
			label="Checkpoint context window (tokens)"
			{hint}
			error={valid ? '' : 'Enter a whole number of tokens.'}
		>
			<input inputmode="numeric" placeholder={String(unsaved ?? providerWindow ?? 1_000_000)} bind:value={draft} />
		</Field>
		{#if errMsg}<Message variant="err">{errMsg}</Message>{/if}
		<div class="actions">
			<Button variant="primary" onclick={save} disabled={!valid || !dirty || saving} loading={saving}>
				{saving ? 'Saving…' : 'Save'}
			</Button>
		</div>
	{/if}
</Section>

<style>
	.hint {
		font-size: var(--text-footnote); color: var(--text-tertiary); line-height: 1.5;
		margin: 0 0 10px;
	}
	.actions { display: flex; justify-content: flex-end; margin-top: 12px; }
	.source { color: var(--text-secondary); }
</style>
