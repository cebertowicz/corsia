<script lang="ts">
	import { activeThreadId } from '$lib/stores/threads';
	import { token as authToken } from '$lib/stores/auth';
	import { coreSettings, loadSettings, type WorkspaceContextFile } from '$lib/stores/settings';
	import { canOpenArtifact, openArtifactAndShowThread } from '$lib/openArtifactInThread';
	import { contextAttachTarget } from '$lib/stores/contextAttach';
	import { plainResponseError } from '$lib/plainError';
	import { Button } from '$lib/components/ui';
	import SkillScopePanel from './SkillScopePanel.svelte';
	import ProvidersPolicyBlock from './ProvidersPolicyBlock.svelte';

	let { onBrowseContextFiles, onBrowseSkills }: {
		onBrowseContextFiles?: () => void;
		onBrowseSkills?: () => void;
	} = $props();

	let settingsError = $state('');
	let newWorkspaceContextPath = $state('');
	let workspaceContextBusy = $state(false);
	const workspaceContextFiles = $derived($coreSettings?.workspace_context_files ?? []);
	const canViewWorkspaceContext = $derived(!!$activeThreadId);

	function contextFileName(file: WorkspaceContextFile): string {
		const raw = file.name || file.path;
		return raw.split(/[\\/]/).filter(Boolean).at(-1) || raw;
	}

	function contextFilePath(file: WorkspaceContextFile): string | null {
		const path = file.path || '';
		if (!path || path === contextFileName(file)) return null;
		return path;
	}

	function openWorkspaceContextFile(file: WorkspaceContextFile) {
		if (!file.exists || !canOpenArtifact()) return;
		openArtifactAndShowThread({ path: file.path, title: contextFileName(file) });
	}

	async function patchWorkspaceContextFiles(files: string[]) {
		const token = $authToken;
		if (!token || workspaceContextBusy) return;
		workspaceContextBusy = true;
		settingsError = '';
		try {
			const res = await fetch('/api/context/bootloader', {
				method: 'PATCH',
				headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
				body: JSON.stringify({ scope: 'workspace', files }),
			});
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not save these settings. Try again.'));
			await loadSettings();
		} catch (exc) {
			settingsError = exc instanceof Error ? exc.message : 'Failed to update workspace context';
		} finally {
			workspaceContextBusy = false;
		}
	}

	function browseWorkspaceContextFiles() {
		if (workspaceContextBusy) return;
		contextAttachTarget.set({
			scope: 'workspace',
			targetLabel: 'Workspace context',
			existingFiles: workspaceContextFiles.map((file) => file.path),
			returnTo: 'workspace-settings',
		});
		onBrowseContextFiles?.();
	}

	async function createWorkspaceContextFile() {
		const token = $authToken;
		const path = newWorkspaceContextPath.trim();
		if (!token || !path || workspaceContextBusy) return;
		workspaceContextBusy = true;
		settingsError = '';
		try {
			const res = await fetch('/api/context/bootloader/file', {
				method: 'POST',
				headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
				body: JSON.stringify({ scope: 'workspace', path, content: `# ${path}\n` }),
			});
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not create that. Try again.'));
			newWorkspaceContextPath = '';
			await loadSettings();
		} catch (exc) {
			settingsError = exc instanceof Error ? exc.message : 'Failed to create workspace context';
		} finally {
			workspaceContextBusy = false;
		}
	}
</script>

<div class="ws-scroll">
	<div class="ws">
		<h2>Workspace settings</h2>
		{#if settingsError}<div class="settings-error">{settingsError}</div>{/if}

		<section class="sec">
			<div class="sec-cap">Workspace context</div>
			<p class="sec-hint">Files every thread receives before project and thread context.</p>
			<div class="context-file-list">
				{#each workspaceContextFiles as file (file.source_id)}
					{@const displayName = contextFileName(file)}
					{@const displayPath = contextFilePath(file)}
					<div class="context-file-row" class:missing={!file.exists}>
						<div class="context-file-main">
							<span class="context-file-name" title={file.path}>{displayName}</span>
							{#if displayPath}<span class="context-file-path" title={displayPath}>{displayPath}</span>{/if}
						</div>
						<span class="context-file-status">{file.exists ? 'Loaded' : 'Missing'}</span>
						<div class="context-file-actions">
							<Button size="sm" disabled={!file.exists || !canViewWorkspaceContext} title={canViewWorkspaceContext ? undefined : 'Open a thread first — documents open beside a conversation'} ariaLabel="View {displayName}" onclick={() => openWorkspaceContextFile(file)}>View</Button>
							<Button size="sm" variant="ghost" disabled={workspaceContextBusy} ariaLabel="Remove {displayName} from workspace context" onclick={() => patchWorkspaceContextFiles(workspaceContextFiles.map((row) => row.path).filter((path) => path !== file.path))}>Remove</Button>
						</div>
					</div>
				{/each}
				{#if workspaceContextFiles.length === 0}<div class="context-empty">No workspace context files found.</div>{/if}
			</div>
			<div class="context-manage-row">
				<Button size="sm" disabled={workspaceContextBusy} onclick={browseWorkspaceContextFiles}>Add existing</Button>
				<details class="context-advanced">
					<summary>Create new context file</summary>
					<div class="context-create-row">
						<input bind:value={newWorkspaceContextPath} placeholder="File name or relative path" aria-label="New workspace context file" onkeydown={(e) => { if (e.key === 'Enter') void createWorkspaceContextFile(); }} />
						<Button size="sm" disabled={workspaceContextBusy || !newWorkspaceContextPath.trim()} onclick={createWorkspaceContextFile}>Create</Button>
					</div>
				</details>
			</div>
		</section>

		<ProvidersPolicyBlock />

		<section class="sec">
			<div class="sec-cap">Workspace skills</div>
			<p class="sec-hint">Workspace-level skills available to every project and thread unless a narrower scope overrides them.</p>
			<SkillScopePanel scope="workspace" scopeLabel="Workspace" inheritanceHint="Inherited by all projects and threads. Project or thread skills with the same name override these." {onBrowseSkills} />
		</section>
	</div>
</div>

<style>
	.ws-scroll { flex: 1; min-height: 0; overflow-y: auto; padding-top: var(--header-bottom); }
	.ws { width: 100%; max-width: 600px; margin: 0 auto; display: flex; flex-direction: column; gap: 12px; padding: 16px 20px 40px; text-align: left; }
	h2 { font-size: var(--text-title2); margin: 0; color: var(--text-primary); }
	.sec { border: 1px solid var(--border-subtle); border-radius: var(--lg-card-radius); background: var(--bg-secondary); padding: 16px 18px; display: flex; flex-direction: column; gap: 11px; }
	.sec-cap { font-size: var(--text-caption); font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-tertiary); }
	.sec-hint, .context-empty { font-size: var(--text-footnote); color: var(--text-tertiary); margin: 0; }
	.settings-error { background: color-mix(in srgb, var(--red) 10%, transparent); border: 1px solid color-mix(in srgb, var(--red) 30%, transparent); color: var(--red); border-radius: var(--radius-xs); padding: 10px 14px; font-size: var(--text-subhead); }
	.context-file-list { display: flex; flex-direction: column; gap: 8px; }
	.context-file-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 12px; padding: 10px 12px; background: var(--bg-tertiary); border: 1px solid transparent; border-radius: var(--radius-sm); }
	.context-file-row.missing { border-color: color-mix(in srgb, var(--yellow) 28%, transparent); }
	.context-file-main { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
	.context-file-name, .context-file-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.context-file-name { color: var(--text-primary); font-size: var(--text-subhead); font-weight: 500; }
	.context-file-path, .context-file-status { color: var(--text-muted); font-size: var(--text-footnote); }
	.context-file-actions { display: flex; justify-content: flex-end; gap: 6px; }
	.context-manage-row { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; }
	.context-advanced { width: 100%; border-top: 1px solid var(--border-subtle); padding-top: 10px; }
	.context-advanced summary { cursor: pointer; color: var(--text-secondary); font-size: var(--text-footnote); }
	.context-create-row { display: flex; gap: 8px; margin-top: 10px; }
	.context-create-row input { flex: 1; min-width: 0; font: inherit; font-size: var(--text-subhead); padding: 7px 9px; border-radius: var(--radius-xs); border: 1px solid var(--border); background: var(--bg-primary); color: var(--text-primary); }
	@media (max-width: 640px) { .ws { padding-inline: 12px; } .context-file-row { grid-template-columns: minmax(0, 1fr) auto; } .context-file-status { display: none; } }
</style>
