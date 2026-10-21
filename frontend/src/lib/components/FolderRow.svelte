<script lang="ts">
	import { Button } from '$lib/components/ui';

	type FolderNotice =
		| { kind: 'protected'; folder: string }
		| { kind: 'unwritable'; reason: string }
		| { kind: 'picker_failed' }
		| null;

	interface Props {
		folder: string;
		notice?: FolderNotice;
		changing?: boolean;
		onChange: () => void;
	}

	const { folder, notice = null, changing = false, onChange }: Props = $props();
</script>

<div class="folder" data-folder-blocked={notice?.kind === 'unwritable' ? 'true' : undefined}>
	<p class="line">
		<span class="label">Work folder</span>
		<span class="path">{folder}</span>
		<Button variant="chip" size="sm" loading={changing} disabled={changing} onclick={onChange}>Change</Button>
	</p>
	{#if notice?.kind === 'protected'}
		<p class="note">macOS asks you once to let CORSIA use your {notice.folder} folder. Choose Allow when it does.</p>
	{:else if notice?.kind === 'unwritable'}
		<p class="note error">CORSIA can’t write to this folder. Choose another one.</p>
	{:else if notice?.kind === 'picker_failed'}
		<p class="note error">Couldn’t check that folder. Choose it again.</p>
	{/if}
</div>

<style>
	.folder { margin-top: var(--space-1); }
	.line { margin: 0; display: flex; align-items: center; gap: var(--space-3); font-size: var(--text-subhead); }
	.label { flex: none; color: var(--text-secondary); }
	.path { min-width: 0; color: var(--text-primary); overflow-wrap: anywhere; }
	.note { margin: var(--space-1) 0 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	.error { color: var(--red-text); }
</style>
