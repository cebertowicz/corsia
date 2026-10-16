<script lang="ts">
	import { token as authToken } from '$lib/stores/auth';
	import { coreSettings, loadSettings, type WorkspaceContextFile } from '$lib/stores/settings';
	import { loadProjects, projects } from '$lib/stores/projects';
	import { projectAccent } from '$lib/utils/projectColors';
	import { canOpenArtifact, openArtifactAndShowThread } from '$lib/openArtifactInThread';
	import { contextAttachTarget } from '$lib/stores/contextAttach';
	import { plainResponseError } from '$lib/plainError';
	import SettingsGroup from './SettingsGroup.svelte';
	import SettingsRow from './SettingsRow.svelte';
	import SettingsContextCreate from './SettingsContextCreate.svelte';
	import SettingsProjectContext from './SettingsProjectContext.svelte';
	import { Button } from '$lib/components/ui';

	// Settings › Context: every level's context files on one page, the workspace first.
	let { onBrowseContextFiles }: { onBrowseContextFiles?: () => void } = $props();

	$effect(() => { void loadProjects(); });
	const active = $derived($projects.filter((project) => project.status !== 'archived'));

	let error = $state('');
	let busy = $state(false);
	const files = $derived($coreSettings?.workspace_context_files ?? []);

	function fileName(file: WorkspaceContextFile): string {
		const raw = file.name || file.path;
		return raw.split(/[\\/]/).filter(Boolean).at(-1) || raw;
	}

	function filePath(file: WorkspaceContextFile): string {
		const path = file.path || '';
		return path === fileName(file) ? '' : path;
	}

	function view(file: WorkspaceContextFile) {
		openArtifactAndShowThread({ path: file.path, title: fileName(file) });
	}

	async function request(url: string, method: string, body: object, fallback: string): Promise<boolean> {
		const token = $authToken;
		if (!token || busy) return false;
		busy = true;
		error = '';
		try {
			const res = await fetch(url, {
				method,
				headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
				body: JSON.stringify(body),
			});
			if (!res.ok) throw new Error(await plainResponseError(res, fallback));
			await loadSettings();
			return true;
		} catch (exc) {
			error = exc instanceof Error ? exc.message : fallback;
			return false;
		} finally {
			busy = false;
		}
	}

	function remove(file: WorkspaceContextFile) {
		const kept = files.map((row) => row.path).filter((path) => path !== file.path);
		void request('/api/context/bootloader', 'PATCH', { scope: 'workspace', files: kept }, 'Could not save these settings. Try again.');
	}

	function create(path: string): Promise<boolean> {
		return request('/api/context/bootloader/file', 'POST', { scope: 'workspace', path, content: `# ${path}\n` }, 'Could not create that. Try again.');
	}

	function addExisting() {
		if (busy) return;
		contextAttachTarget.set({
			scope: 'workspace',
			targetLabel: 'Workspace context',
			existingFiles: files.map((file) => file.path),
			returnTo: 'settings',
			returnSection: 'context',
		});
		onBrowseContextFiles?.();
	}
</script>

<SettingsGroup label="All projects" anchor="context-workspace" hint="Every conversation gets these first, before its project's and its own.">
	{#if error}<p class="error" role="alert">{error}</p>{/if}
	{#each files as file (file.source_id)}
		<SettingsRow
			name={fileName(file)}
			sub={file.exists ? filePath(file) : 'Missing'}
			subTone={file.exists ? 'quiet' : 'failed'}
			onOpen={file.exists && canOpenArtifact() ? () => view(file) : undefined}
			menuItems={[{ id: 'remove', label: 'Remove from context', disabled: busy }]}
			onMenuSelect={() => remove(file)}
		/>
	{:else}
		<p class="empty">No context files for all projects yet.</p>
	{/each}
	<div class="actions">
		<Button size="sm" variant="ghost" disabled={busy} onclick={addExisting}>Add existing</Button>
		<SettingsContextCreate label="New context file for all projects" {busy} onCreate={create} />
	</div>
</SettingsGroup>
{#each active as project (project.id)}
	<SettingsGroup label={project.name} mark={projectAccent(project.id, project.accent_color)} anchor={`context-${project.id}`}>
		<SettingsProjectContext projectId={project.id} />
	</SettingsGroup>
{/each}

<style>
	.empty, .error { margin: 0; padding: 0 var(--space-2); font-size: var(--text-caption); color: var(--text-tertiary); }
	.error { color: var(--red-text); }
	.actions { display: flex; flex-wrap: wrap; align-items: flex-start; gap: var(--space-1); margin-top: var(--space-1); }
</style>
