<script lang="ts">
	import { Button } from '$lib/components/ui';

	type FolderWarning = { kind: 'personal_folder'; folder: string } | { kind: 'contains_personal_folder'; folder: string } | { kind: 'contains_files' };
	type FolderNotice =
		| { kind: 'warning'; warning: FolderWarning }
		| { kind: 'unwritable'; reason: string }
		| { kind: 'unreadable'; reason: string }
		| { kind: 'picker_failed' }
		| null;

	interface Props {
		folder: string;
		defaultFolder: string;
		notice?: FolderNotice;
		changing?: boolean;
		onChange: () => void;
		onUseDefault?: () => void;
		onKeepFolder?: () => void;
	}

	const { folder, defaultFolder, notice = null, changing = false, onChange, onUseDefault, onKeepFolder }: Props = $props();
	function folderWarningNotice(folder: FolderWarning): string {
		const safety = 'Agents open anything in the work folder without asking. Outside it, access needs approval.';
		if (folder.kind === 'personal_folder') return `Your ${folder.folder} folder holds personal files. macOS may ask to let CORSIA use it. ${safety}`;
		if (folder.kind === 'contains_personal_folder') return `This folder contains your ${folder.folder} folder. macOS may ask to let CORSIA use that folder. ${safety}`;
		return `This folder already holds files. ${safety}`;
	}
</script>

<div class="folder" data-folder-blocked={notice?.kind === 'unwritable' || notice?.kind === 'unreadable' ? 'true' : undefined}>
	<p class="line">
		<span class="label">Work folder</span>
		<span class="path">{folder}</span>
		<Button variant="chip" size="sm" loading={changing} disabled={changing} onclick={onChange}>Change</Button>
	</p>
	<p class="why">Your projects and files live here.</p>
	{#if notice?.kind === 'warning'}
		<p class="note">{folderWarningNotice(notice.warning)}</p>
		<div class="choices">
			<Button variant="chip" size="sm" onclick={onUseDefault}>Use {defaultFolder}</Button>
			<Button variant="chip" size="sm" onclick={onKeepFolder}>Keep this folder</Button>
		</div>
	{:else if notice?.kind === 'unwritable'}
		<p class="note error">CORSIA couldn’t write to this folder. Choose another one.</p>
	{:else if notice?.kind === 'unreadable'}
		<p class="note error">CORSIA couldn’t inspect this folder. Choose another one.</p>
	{:else if notice?.kind === 'picker_failed'}
		<p class="note error">Couldn’t check that folder. Choose it again.</p>
	{/if}
</div>

<style>
	.folder { margin-top: var(--space-1); }
	.line { margin: 0; display: flex; align-items: center; gap: var(--space-3); font-size: var(--text-subhead); }
	.label { flex: none; color: var(--text-secondary); }
	.path { min-width: 0; color: var(--text-primary); overflow-wrap: anywhere; }
	.why { margin: var(--space-1) 0 0; color: var(--text-secondary); font-size: var(--text-subhead); }
	.note { margin: var(--space-1) 0 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	.choices { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-2); }
	.error { color: var(--red-text); }
</style>
