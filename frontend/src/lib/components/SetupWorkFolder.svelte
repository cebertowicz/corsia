<script lang="ts">
	import FolderRow from './FolderRow.svelte';

	export type FolderNotice = { kind: 'protected'; folder: string } | { kind: 'unwritable'; reason: string } | { kind: 'picker_failed' } | null;
	type Choice = { state: 'ready'; path: string; protected_folder: string | null } | { state: 'unwritable'; path: string; reason: string };
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
</script>
<FolderRow {folder} {notice} {changing} onChange={changeFolder} />
{#if error}<p class="setup-error">{error}</p>{/if}

<style>
	.setup-error { margin: var(--space-2) 0 0; color: var(--red-text); font-size: var(--text-footnote); line-height: var(--lh-normal); }
</style>
