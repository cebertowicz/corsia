<script lang="ts">
	import { openArtifactAndShowThread } from '$lib/openArtifactInThread';
	import { Button } from '$lib/components/ui';
	import { createSkill, fetchSkills, type SkillRow, type SkillScope } from '$lib/api/skills';

	let {
		scope,
		scopeLabel,
		inheritanceHint,
		projectId = null,
		threadId = null,
		onBrowseSkills,
		onOpenSkill,
	}: {
		scope: SkillScope;
		scopeLabel: string;
		inheritanceHint: string;
		projectId?: string | null;
		threadId?: string | null;
		onBrowseSkills?: () => void;
		onOpenSkill?: () => void;
	} = $props();

	let rows = $state<SkillRow[]>([]);
	let loading = $state(false);
	let error = $state('');
	let loadedKey = $state('');
	let createOpen = $state(false);
	let newName = $state('');
	let newDescription = $state('');
	let creating = $state(false);

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
	// (this panel's thread when it has one, else the active thread) rather than
	// in a standalone editor with nowhere for its comments to go.
	function openSkill(row: SkillRow) {
		const opened = openArtifactAndShowThread(
			{ path: row.path, title: row.name || displayName(row.path) },
			threadId,
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
	<div class="skills-scope">Managing skills for {scopeLabel}</div>
	<div class="skills-summary">
		<span>{countLabel}</span>
		<span class:bad={invalidRows.length > 0}>{invalidLabel}</span>
	</div>
	<p class="skills-hint">{inheritanceHint}</p>

	{#if error}
		<div class="skills-error" role="alert">{error}</div>
	{/if}

	{#if loading}
		<div class="skills-empty">Loading skills...</div>
	{:else if rows.length === 0}
		<div class="skills-empty">No local skills in this scope.</div>
	{:else}
		<div class="skills-list">
			{#each validRows as row (row.path)}
				<div class="skill-row">
					<div class="skill-main">
						<span class="skill-name">{row.name}</span>
						{#if row.description}
							<span class="skill-desc">{row.description}</span>
						{/if}
					</div>
					<Button size="sm" onclick={() => openSkill(row)}>Open</Button>
				</div>
			{/each}
			{#each invalidRows as row (row.path)}
				<div class="skill-row invalid">
					<div class="skill-main">
						<span class="skill-name">{row.name}</span>
						<span class="skill-desc">Omitted: {row.errors.join(', ')}</span>
					</div>
					<Button size="sm" variant="ghost" onclick={() => openSkill(row)}>Repair</Button>
				</div>
			{/each}
		</div>
	{/if}

	<div class="skills-actions">
		<Button size="sm" disabled={loading} onclick={() => onBrowseSkills?.()}>Browse</Button>
		<Button
			size="sm"
			variant={createOpen ? 'ghost' : 'secondary'}
			disabled={loading || creating}
			onclick={() => (createOpen = !createOpen)}
		>{createOpen ? 'Cancel' : 'Create skill'}</Button>
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
	.skills-panel {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.skills-scope {
		color: var(--text-secondary);
		font-size: var(--text-subhead);
		font-weight: 600;
	}

	.skills-summary {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
	}

	.skills-summary span {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-xs);
		padding: 2px 7px;
	}

	.skills-summary .bad {
		color: var(--red);
		border-color: color-mix(in srgb, var(--red) 35%, transparent);
		background: var(--red-bg);
	}

	.skills-hint,
	.skills-empty {
		margin: 0;
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
	}

	.skills-error {
		border: 1px solid color-mix(in srgb, var(--red) 35%, transparent);
		border-radius: var(--radius-sm);
		background: var(--red-bg);
		color: var(--red);
		font-size: var(--text-footnote);
		padding: 8px 10px;
	}

	.skills-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.skill-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: center;
		gap: var(--space-2);
		min-height: 34px;
	}

	.skill-row.invalid {
		border-left: 2px solid var(--red);
		padding-left: var(--space-2);
	}

	.skill-main {
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.skill-name {
		color: var(--text-primary);
		font-size: var(--text-subhead);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.skill-desc {
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.skills-actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
	}

	.skill-create {
		display: grid;
		grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.4fr) auto;
		gap: var(--space-2);
	}

	.skill-create input {
		min-width: 0;
		font: inherit;
		font-size: var(--text-subhead);
		padding: 7px 10px;
		border-radius: var(--radius-sm);
		border: 1px solid var(--border);
		background: var(--bg-primary);
		color: var(--text-primary);
	}

	@media (max-width: 640px) {
		.skill-create {
			grid-template-columns: minmax(0, 1fr);
		}
	}
</style>
