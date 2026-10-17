<script lang="ts">
	import Modal from './ui/Modal.svelte';
	import Button from './ui/Button.svelte';
	import AccentPicker from './ui/AccentPicker.svelte';
	import { createProject, type ProjectInfo } from '$lib/stores/projects';
	import { createProjectOpen } from '$lib/stores/overlays';

	let { onCreated }: { onCreated?: (p: ProjectInfo) => void } = $props();

	let name = $state('');
	let description = $state('');
	let accent = $state<string | null>(null);
	let accentPreview = $state<string | null>(null);
	let busy = $state(false);
	let error = $state<string | null>(null);

	const canCreate = $derived(name.trim().length > 0 && !busy);

	function reset() {
		name = '';
		description = '';
		accent = null;
		accentPreview = null;
		busy = false;
		error = null;
	}
	function close() {
		createProjectOpen.set(false);
		reset();
	}

	async function submit() {
		if (!canCreate) return;
		busy = true;
		error = null;
		try {
			const proj = await createProject({
				name: name.trim(),
				description: description.trim(),
				accent_color: accent,
			});
			onCreated?.(proj);
			close();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not create project';
			busy = false;
		}
	}
</script>

{#if $createProjectOpen}
	<Modal title="New project" size="sm" onClose={close}>
		<div class="form">
			<label class="fld">
				<span>Name</span>
				<!-- svelte-ignore a11y_autofocus -->
				<input
					type="text"
					bind:value={name}
					placeholder="e.g. Acme Redesign"
					autofocus
					onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } }}
				/>
			</label>

			<label class="fld">
				<span>Description <em>optional</em></span>
				<textarea
					rows="2"
					bind:value={description}
					placeholder="Short description — shown in the projects list…"
				></textarea>
			</label>

			<div class="fld">
				<span>Accent</span>
				<AccentPicker
					value={accentPreview ?? accent}
					onPick={(c) => { accent = c; accentPreview = null; }}
					onPreview={(c) => (accentPreview = c)}
				/>
			</div>

			{#if error}
				<div class="err" role="alert">{error}</div>
			{/if}
		</div>

		{#snippet foot()}
			<Button variant="ghost" onclick={close} disabled={busy}>Cancel</Button>
			<Button variant="primary" onclick={submit} disabled={!canCreate}>
				{busy ? 'Creating…' : 'Create project'}
			</Button>
		{/snippet}
	</Modal>
{/if}

<style>
	.form { display: flex; flex-direction: column; gap: 14px; }

	.fld { display: flex; flex-direction: column; gap: 6px; }
	.fld > span:first-child {
		font-size: var(--text-footnote); color: var(--text-secondary); display: flex; gap: 6px; align-items: baseline;
	}
	.fld em { font-style: normal; font-size: var(--text-footnote); color: var(--text-tertiary); }
	.fld input, .fld textarea {
		font: inherit; font-size: var(--text-subhead); padding: 9px 11px;
		border-radius: var(--radius-sm); border: 1px solid var(--border);
		background: var(--bg-primary); color: var(--text-primary); resize: vertical;
	}
	.fld input:focus, .fld textarea:focus { outline: none; border-color: var(--accent); }

	.err {
		font-size: var(--text-subhead); color: var(--red-text);
		background: color-mix(in srgb, var(--red) 8%, transparent);
		border: 1px solid color-mix(in srgb, var(--red) 25%, transparent);
		border-radius: var(--radius-sm); padding: 8px 10px;
	}
</style>
