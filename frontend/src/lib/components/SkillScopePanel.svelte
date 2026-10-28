<script lang="ts">
	import { openArtifactAndShowThread } from '$lib/openArtifactInThread';
	import { Button } from '$lib/components/ui';
	import SettingsRow from './SettingsRow.svelte';
	import { createSkill, fetchSkills, type SkillRow, type SkillScope } from '$lib/api/skills';
	import { createScopeDocumentHome } from '$lib/scopeDocumentHome.svelte';

	let {
		scope,
		scopeLabel,
		inheritanceHint,
		projectId = null,
		threadId = null,
		onBrowseSkills,
		onOpenSkill,
		heading = true,
	}: {
		scope: SkillScope;
		scopeLabel: string;
		inheritanceHint: string;
		projectId?: string | null;
		threadId?: string | null;
		onBrowseSkills?: () => void;
		onOpenSkill?: () => void;
		/** Settings names the level in its own section label. */
		heading?: boolean;
	} = $props();

	let rows = $state<SkillRow[]>([]);
	let loading = $state(false);
	let error = $state('');
	let loadedKey = $state('');
	let createOpen = $state(false);
	let newName = $state('');
	let newDescription = $state('');
	let creating = $state(false);

	// With no thread on screen (Settings opened from Activity) a skill opens beside its scope's own thread.
	const home = createScopeDocumentHome(() => (scope === 'project' ? projectId : null), () => threadId);

	const invalidRows = $derived(rows.filter((row) => row.status === 'error'));
	const validRows = $derived(rows.filter((row) => row.status !== 'error'));
	const countLabel = $derived(`${rows.length} local ${rows.length === 1 ? 'skill' : 'skills'}`);
	const invalidLabel = $derived(
		invalidRows.length ? `${invalidRows.length} invalid` : 'No invalid skills',
	);

	$effect(() => {
		const key = `${scope}:${projectId ?? ''}:${threadId ?? ''}`;
		if (key === loadedKey) return;
		loadedKey = key;
		void load();
	});

	async function load() {
		if (scope === 'project' && !projectId) {
			rows = [];
			return;
		}
		if (scope === 'thread' && !threadId) {
			rows = [];
			return;
		}
		loading = true;
		error = '';
		try {
			const data = await fetchSkills({ scope, projectId, threadId });
			rows = Array.isArray(data.skills) ? data.skills : [];
		} catch (exc) {
			error = exc instanceof Error ? exc.message : 'Could not load skills';
		} finally {
			loading = false;
		}
	}

	function displayName(path: string): string {
		return path.split(/[\\/]/).filter(Boolean).at(-2) ?? path;
	}

	// A skill file is editable and commentable, so it opens as a tab in a thread
	// (this panel's thread when it has one, else the active thread, else its scope's
	// thread) rather than in a standalone editor with nowhere for its comments to go.
	function openSkill(row: SkillRow) {
		const opened = openArtifactAndShowThread(
			{ path: row.path, title: row.name || displayName(row.path) },
			threadId,
			home.thread,
		);
		if (opened) onOpenSkill?.();
	}

	async function create() {
		if (creating || !newName.trim() || !newDescription.trim()) return;
		creating = true;
		error = '';
		try {
			const row = await createSkill({
				scope,
				name: newName.trim(),
				description: newDescription.trim(),
				projectId,
				threadId,
			});
			rows = [...rows, row].sort((a, b) => a.name.localeCompare(b.name));
			newName = '';
			newDescription = '';
			createOpen = false;
			openSkill(row);
		} catch (exc) {
			error = exc instanceof Error ? exc.message : 'Could not create skill';
		} finally {
			creating = false;
		}
	}
</script>

<div class="skills-panel">
	{#if heading}<div class="skills-scope">Managing skills for {scopeLabel}</div>{/if}
	<p class="skills-summary">
		{countLabel}<span class="sep" aria-hidden="true">{' · '}</span><span class:bad={invalidRows.length > 0}>{invalidLabel}</span>
	</p>
	<p class="skills-hint">{inheritanceHint}</p>

	{#if error}
		<p class="skills-error" role="alert">{error}</p>
	{/if}

	{#if loading}
		<div class="skills-empty">Loading skills...</div>
	{:else if rows.length === 0}
		<div class="skills-empty">No skills saved yet.</div>
	{:else}
		<div class="skills-list">
			{#each validRows as row (row.path)}
				<SettingsRow name={row.name} sub={row.description ?? ''} onOpen={home.canOpen ? () => openSkill(row) : undefined} />
			{/each}
			{#each invalidRows as row (row.path)}
				<SettingsRow name={row.name} sub={`Omitted: ${row.errors.join(', ')}`} subTone="failed" onOpen={home.canOpen ? () => openSkill(row) : undefined} />
			{/each}
		</div>
		{#if home.unavailable}<p class="skills-empty">Open a chat to view this.</p>{/if}
		{#if home.failed}
			<p class="skills-error" role="alert">Couldn't find where to open these. <Button size="sm" variant="ghost" onclick={home.retry}>Retry</Button></p>
		{/if}
	{/if}

	<div class="skills-actions">
		<Button size="sm" variant="ghost" disabled={loading} onclick={() => onBrowseSkills?.()}>Browse</Button>
		<Button size="sm" variant="ghost" disabled={loading || creating} onclick={() => (createOpen = !createOpen)}>{createOpen ? 'Cancel' : 'Create skill'}</Button>
		<Button size="sm" variant="ghost" disabled={loading} onclick={load}>Refresh</Button>
	</div>

	{#if createOpen}
		<div class="skill-create">
			<input
				bind:value={newName}
				placeholder="skill-name"
				aria-label="New skill name"
				onkeydown={(event) => { if (event.key === 'Enter') void create(); }}
			/>
			<input
				bind:value={newDescription}
				placeholder="Use when..."
				aria-label="New skill description"
				onkeydown={(event) => { if (event.key === 'Enter') void create(); }}
			/>
			<Button size="sm" disabled={creating || !newName.trim() || !newDescription.trim()} onclick={create}>
				{creating ? 'Creating...' : 'Create'}
			</Button>
		</div>
	{/if}
</div>

<style>
	.skills-panel { display: flex; flex-direction: column; gap: var(--space-2); }
	/* Text starts where a row's label does. */
	.skills-scope, .skills-summary, .skills-hint, .skills-empty, .skills-error { padding: 0 var(--space-2); }
	.skills-scope { color: var(--text-tertiary); font-size: var(--text-footnote); font-weight: var(--fw-semibold); }
	.skills-summary, .skills-hint, .skills-empty, .skills-error { margin: 0; font-size: var(--text-caption); color: var(--text-tertiary); }
	.skills-summary .bad, .skills-error { color: var(--red-text); }
	.skills-list { display: flex; flex-direction: column; }
	/* Settings action labels share the group label's row-text edge without changing Button globally. */
	.skills-actions { display: flex; flex-wrap: wrap; gap: var(--space-1); padding-left: var(--space-2); margin-left: calc(-1 * var(--button-pad-x-sm)); }
	.skill-create { display: grid; grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.4fr) auto; gap: var(--space-2); }
	.skill-create input {
		min-width: 0;
		font: inherit; font-size: var(--text-subhead);
		padding: var(--space-2) var(--space-3);
		border-radius: var(--radius-sm); border: 1px solid var(--border);
		background: var(--bg-primary); color: var(--text-primary);
	}
	@media (max-width: 640px) {
		.skill-create { grid-template-columns: minmax(0, 1fr); }
	}
</style>
