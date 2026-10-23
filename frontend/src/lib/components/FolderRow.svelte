<script lang="ts">
	import { Button } from '$lib/components/ui';

	type ProtectedFolder = { kind: 'personal_folder'; folder: string } | { kind: 'contains_personal_folder'; folder: string } | { kind: 'contains_files' };
	type FolderNotice =
		| { kind: 'protected'; folder: ProtectedFolder }
		| { kind: 'unwritable'; reason: string }
		| { kind: 'picker_failed' }
		| null;

	interface Props {
		folder: string;
		notice?: FolderNotice;
		changing?: boolean;
		onChange: () => void;
		onUseDefault?: () => void;
		onKeepProtected?: () => void;
	}

	const { folder, notice = null, changing = false, onChange, onUseDefault, onKeepProtected }: Props = $props();
	function protectedNotice(folder: ProtectedFolder): string {
		const safety = 'Agents open anything in the work folder without asking; outside it, CORSIA asks you first.';
		if (folder.kind === 'personal_folder') return `Your ${folder.folder} folder holds personal files. ${safety}`;
		if (folder.kind === 'contains_personal_folder') return `This folder contains your ${folder.folder} folder. ${safety}`;
		return `This folder already holds files. ${safety}`;
	}
</script>

<div class="folder" data-folder-blocked={notice?.kind === 'unwritable' ? 'true' : undefined}>
	<p class="line">
		<span class="label">Work folder</span>
		<span class="path">{folder}</span>
		<Button variant="chip" size="sm" loading={changing} disabled={changing} onclick={onChange}>Change</Button>
	</p>
	<p class="why">Your projects and files live here. macOS asks once to let CORSIA use this folder; choose Allow.</p>
	{#if notice?.kind === 'protected'}
		<p class="note">{protectedNotice(notice.folder)}</p>
		<div class="choices">
			<Button variant="chip" size="sm" onclick={onUseDefault}>Use ~/CORSIA</Button>
			<Button variant="chip" size="sm" onclick={onKeepProtected}>Keep this folder</Button>
		</div>
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
	.why { margin: var(--space-1) 0 0; color: var(--text-secondary); font-size: var(--text-subhead); }
	.note { margin: var(--space-1) 0 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	.choices { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-2); }
	.error { color: var(--red-text); }
</style>
