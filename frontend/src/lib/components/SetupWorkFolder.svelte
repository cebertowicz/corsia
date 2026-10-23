<script lang="ts">
	import FolderRow from './FolderRow.svelte';

	type FolderWarning = { kind: 'personal_folder'; folder: string } | { kind: 'contains_personal_folder'; folder: string } | { kind: 'contains_files' };
	export type FolderNotice = { kind: 'warning'; warning: FolderWarning } | { kind: 'unwritable'; reason: string } | { kind: 'unreadable'; reason: string } | { kind: 'picker_failed' } | null;
	type Choice = { state: 'ready'; path: string; folder_warning: FolderWarning | null } | { state: 'unwritable'; path: string; reason: string } | { state: 'unreadable'; path: string; reason: string };
	interface Props { folder: string; defaultFolder: string; defaultFolderPath: string; applyPath: string; savedFolder: boolean; changed: boolean; changing: boolean; notice: FolderNotice; error?: string }
	let { folder = $bindable(), defaultFolder, defaultFolderPath, applyPath = $bindable(), savedFolder, changed = $bindable(), changing = $bindable(), notice = $bindable(), error = '' }: Props = $props();
	async function changeFolder() {
		const invoke = (window as Window & { __TAURI_INTERNALS__?: { invoke: (command: string) => Promise<unknown> } }).__TAURI_INTERNALS__?.invoke;
		if (!invoke || changing) return;
		changing = true;
		try {
			const choice = await invoke('choose_work_folder') as Choice | null;
			if (!choice) return;
			folder = choice.path; applyPath = choice.path; changed = true;
			notice = choice.state === 'ready' ? (choice.folder_warning ? { kind: 'warning', warning: choice.folder_warning } : null) : { kind: choice.state, reason: choice.reason };
		} catch { notice = { kind: 'picker_failed' }; }
		finally { changing = false; }
	}

	function useDefaultFolder() {
		folder = defaultFolder;
		applyPath = defaultFolderPath;
		// The shell already owns an initial default workspace. A saved personal folder must move back to it first.
		changed = savedFolder;
		notice = null;
	}

	function keepFolder() { notice = null; }
</script>
<FolderRow {folder} {defaultFolder} {notice} {changing} onChange={changeFolder} onUseDefault={useDefaultFolder} onKeepFolder={keepFolder} />
{#if error}<p class="error-text" role="alert">{error}</p>{/if}

<style>
	.error-text { margin: var(--space-2) 0 0; font-size: var(--text-subhead); }
</style>
