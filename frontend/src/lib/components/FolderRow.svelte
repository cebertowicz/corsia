<script lang="ts">
	import { Button } from '$lib/components/ui';

	type FolderNotice =
		| { kind: 'protected'; folder: string }
		| { kind: 'unwritable'; reason: string }
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
		<p class="note">macOS will ask before CORSIA can use {notice.folder}.</p>
	{:else if notice?.kind === 'unwritable'}
		<p class="note error">CORSIA can’t write here: {notice.reason}.</p>
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
