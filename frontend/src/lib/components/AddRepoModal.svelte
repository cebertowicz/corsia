<script lang="ts">
	import Modal from './ui/Modal.svelte';
	import Toggle from './ui/Toggle.svelte';

	let {
		open,
		mode,
		pinCurrent,
		protectBaseCheckout,
		path,
		name,
		busy,
		error,
		canSubmit,
		onModeChange,
		onPathChange,
		onNameChange,
		onPinChange,
		onProtectChange,
		onBrowse,
		onSubmit,
		onCancel,
	}: {
		open: boolean;
		mode: 'link' | 'create';
		pinCurrent: boolean;
		protectBaseCheckout: boolean;
		path: string;
		name: string;
		busy: boolean;
		error: string | null;
		canSubmit: boolean;
		onModeChange: (mode: 'link' | 'create') => void;
		onPathChange: (path: string) => void;
		onNameChange: (name: string) => void;
		onPinChange: (next: boolean) => void;
		onProtectChange: (next: boolean) => void;
		onBrowse: () => void;
		onSubmit: () => void;
		onCancel: () => void;
	} = $props();
</script>

{#if open}
	<Modal title="Add repository" size="sm" onClose={onCancel}>
		<div class="form">
			<div class="seg" role="tablist" aria-label="Add mode">
				<button role="tab" aria-selected={mode === 'link'} class:on={mode === 'link'} onclick={() => onModeChange('link')}>Link existing</button>
				<button role="tab" aria-selected={mode === 'create'} class:on={mode === 'create'} onclick={() => onModeChange('create')}>Create new</button>
			</div>

			{#if mode === 'link'}
				<label class="fld">
					<span>Path to the repository</span>
					<div class="pathrow">
						<input
							type="text"
							value={path}
							oninput={(event) => onPathChange(event.currentTarget.value)}
							aria-label="Repository path"
							placeholder="~/code/my-repo"
						/>
						<button class="btn ghost" onclick={onBrowse}>Browse…</button>
					</div>
				</label>
				<label class="fld">
					<span>Name <em>optional — defaults from the folder</em></span>
					<input type="text" value={name} oninput={(event) => onNameChange(event.currentTarget.value)} placeholder="my-repo" />
				</label>
				<p class="note">Linked into <span class="mono">source/</span> — the repository itself is never moved or modified.</p>
			{:else}
				<label class="fld">
					<span>Name</span>
					<input type="text" value={name} oninput={(event) => onNameChange(event.currentTarget.value)} placeholder="my-new-repo" />
				</label>
				<p class="note">Creates <span class="mono">source/&lt;name&gt;/</span> in this project and runs <span class="mono">git init</span>.</p>
			{/if}

			<div class="add-settings" aria-label="Repository defaults">
				<div class="add-setting">
					<div class="setting-copy">
						<span class="setting-title">Pin current branch</span>
						<span class="setting-detail">Default for Git repositories</span>
					</div>
					<Toggle
						checked={pinCurrent}
						onchange={onPinChange}
						ariaLabel="Pin current branch for added repository"
						size="sm"
					/>
				</div>
				<div class="add-setting">
					<div class="setting-copy">
						<span class="setting-title">Protect base checkout</span>
						<span class="setting-detail">Default for Git repositories</span>
					</div>
					<Toggle
						checked={protectBaseCheckout}
						onchange={onProtectChange}
						ariaLabel="Protect base checkout for added repository"
						size="sm"
					/>
				</div>
			</div>

			{#if error}
				<div class="err" role="alert">{error}</div>
			{/if}
		</div>

		{#snippet foot()}
			<button class="btn ghost push" onclick={onCancel} disabled={busy}>Cancel</button>
			<button class="btn primary" onclick={onSubmit} disabled={!canSubmit}>
				{busy ? 'Adding…' : mode === 'link' ? 'Link repository' : 'Create repository'}
			</button>
		{/snippet}
	</Modal>
{/if}

<style>
	.form { display: flex; flex-direction: column; gap: 13px; }
	.seg { display: flex; gap: 4px; background: var(--bg-active); border-radius: var(--radius-sm); padding: 3px; align-self: flex-start; }
	.seg button { font: inherit; font-size: var(--text-footnote); font-weight: 600; padding: 5px 13px; border: none; border-radius: var(--radius-xs); background: none; cursor: pointer; color: var(--text-secondary); }
	.seg button.on { background: var(--bg-primary); color: var(--text-primary); box-shadow: 0 1px 3px rgba(0,0,0,0.12); }
	.fld { display: flex; flex-direction: column; gap: 6px; }
	.fld > span:first-child { font-size: var(--text-footnote); color: var(--text-secondary); display: flex; gap: 6px; align-items: baseline; }
	.fld em { font-style: normal; font-size: var(--text-footnote); color: var(--text-tertiary); }
	.fld input { font: inherit; font-size: var(--text-subhead); padding: 9px 11px; border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--bg-primary); color: var(--text-primary); width: 100%; }
	.fld input:focus { outline: none; border-color: var(--accent); }
	.pathrow { display: flex; gap: 8px; }
	.pathrow input { flex: 1; }
	.add-settings { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
	.add-setting { display: flex; align-items: center; gap: 8px; min-width: 0; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-primary); padding: 7px 9px; }
	.setting-copy { display: flex; flex-direction: column; gap: 1px; flex: 1; min-width: 0; }
	.setting-title { font-size: var(--text-footnote); color: var(--text-secondary); }
	.setting-detail { font-size: var(--text-caption); color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.note { font-size: var(--text-footnote); color: var(--text-tertiary); margin: 0; }
	.err { font-size: var(--text-subhead); color: var(--red); background: color-mix(in srgb, var(--red) 8%, transparent); border: 1px solid color-mix(in srgb, var(--red) 25%, transparent); border-radius: var(--radius-sm); padding: 8px 10px; }
	.btn { font: inherit; font-size: var(--text-subhead); font-weight: 500; padding: 8px 16px; border-radius: var(--radius-sm); cursor: pointer; border: 1px solid transparent; }
	.btn.push { margin-left: auto; }
	.btn.ghost { background: none; border-color: var(--border); color: var(--text-secondary); }
	.btn.ghost:hover { color: var(--text-primary); background: var(--bg-hover); }
	.btn.primary { background: var(--accent); color: #fff; }
	.btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.mono { font-family: var(--font-mono, monospace); }
	@media (max-width: 640px) { .add-settings { grid-template-columns: 1fr; } .pathrow { flex-direction: column; } .pathrow .btn { align-self: flex-start; } }
</style>
