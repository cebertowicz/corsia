<script lang="ts">
	import FolderRow from './FolderRow.svelte';

	type ProtectedFolder = { kind: 'personal_folder'; folder: string } | { kind: 'contains_personal_folder'; folder: string } | { kind: 'contains_files' };
	export type FolderNotice = { kind: 'protected'; folder: ProtectedFolder } | { kind: 'unwritable'; reason: string } | { kind: 'picker_failed' } | null;
	type Choice = { state: 'ready'; path: string; protected_folder: ProtectedFolder | null } | { state: 'unwritable'; path: string; reason: string };
	interface Props { folder: string; changed: boolean; changing: boolean; notice: FolderNotice; error?: string }
	let { folder = $bindable(), changed = $bindable(), changing = $bindable(), notice = $bindable(), error = '' }: Props = $props();
	async function changeFolder() {
		const invoke = (window as Window & { __TAURI_INTERNALS__?: { invoke: (command: string) => Promise<unknown> } }).__TAURI_INTERNALS__?.invoke;
		if (!invoke || changing) return;
		changing = true;
		try {
			const choice = await invoke('choose_work_folder') as Choice | null;
			if (!choice) return;
			folder = choice.path; changed = true;
			notice = choice.state === 'ready' ? (choice.protected_folder ? { kind: 'protected', folder: choice.protected_folder } : null) : { kind: 'unwritable', reason: choice.reason };
		} catch { notice = { kind: 'picker_failed' }; }
		finally { changing = false; }
	}

	function useDefaultFolder() {
		folder = '~/CORSIA';
		// The desktop shell owns and creates its default workspace; do not send a display path back as a filesystem path.
		changed = false;
		notice = null;
	}

	function keepProtectedFolder() { notice = null; }
</script>
<FolderRow {folder} {notice} {changing} onChange={changeFolder} onUseDefault={useDefaultFolder} onKeepProtected={keepProtectedFolder} />
{#if error}<p class="error-text" role="alert">{error}</p>{/if}

<style>
	.error-text { margin: var(--space-2) 0 0; font-size: var(--text-subhead); }
</style>
