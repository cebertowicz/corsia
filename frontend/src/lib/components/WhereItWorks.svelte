<script lang="ts">
	import { projects, loadProjects } from '$lib/stores/projects';
	import { Button, Modal } from '$lib/components/ui';
	import { saveConnectedAppReach, type ConnectedApp } from '$lib/api/connectedApps';

	let { app, onClose, onSaved }: { app: ConnectedApp; onClose: () => void; onSaved: (app: ConnectedApp) => void } = $props();
	let mode = $state<'all' | 'selected'>(app.reach.mode);
	let selected = $state(new Set(app.reach.project_ids));
	let outsideProjects = $state(app.reach.outside_projects);
	let saving = $state(false);
	let error = $state('');

	void loadProjects();

	const hasSelection = $derived(mode === 'all' || selected.size > 0 || outsideProjects);
	const projectNames = $derived($projects.filter((project) => selected.has(project.id)).map((project) => project.name));
	const selectedNames = $derived([...projectNames, ...(outsideProjects ? ['Outside projects'] : [])]);
	const allNames = $derived([...$projects.map((project) => project.name), 'Outside projects']);
	const wideningLine = $derived(`Agents in ${allNames.join(', ')} and any new project will be able to use ${app.name}.`);
	const originalNames = $derived([
		...$projects.filter((project) => app.reach.project_ids.includes(project.id)).map((project) => project.name),
		...(app.reach.outside_projects ? ['Outside projects'] : []),
	]);
	const narrowingLine = $derived.by(() => {
		if (app.reach.mode === 'all') {
			const losing = allNames.filter((name) => !selectedNames.includes(name));
			return `Agents in ${losing.join(', ')} won't be able to use ${app.name}. New projects won't get it.`;
		}
		const from = originalNames.join(', ');
		const to = selectedNames.join(', ');
		return `${app.name} will move from ${from} to ${to}, with its sign-in and key. Agents in ${from} won't be able to use it.`;
	});
	const disabledLine = $derived(`Select at least one project, or remove ${app.name}.`);

	function selectAllProjects(): void {
		mode = 'all';
	}

	function selectIndividually(): void {
		mode = 'selected';
	}

	function toggleProject(id: string): void {
		const next = new Set(selected);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selected = next;
	}

	async function save(): Promise<void> {
		if (!hasSelection) return;
		saving = true;
		error = '';
		try {
			const reach = mode === 'all'
				? { mode: 'all' as const, project_ids: [], outside_projects: false }
				: { mode: 'selected' as const, project_ids: [...selected], outside_projects: outsideProjects };
			onSaved(await saveConnectedAppReach(app, reach));
		} catch (cause) {
			error = cause instanceof Error ? cause.message : 'Could not update this app. Try again.';
		} finally {
			saving = false;
		}
	}
</script>

<Modal title={`Where ${app.name} works`} size="sm" {onClose}>
	<div class="where-it-works">
		<label class="choice">
			<input type="radio" name="reach" checked={mode === 'all'} onchange={selectAllProjects} />
			<span><strong>All projects</strong><small>Includes new projects and Outside projects</small></span>
		</label>
		{#if mode === 'all'}
			<p class="impact">{wideningLine}</p>
		{/if}
		<label class="choice">
			<input type="radio" name="reach" checked={mode === 'selected'} onchange={selectIndividually} />
			<span><strong>Select individually</strong></span>
		</label>
		{#if mode === 'selected'}
			<div class="checklist">
				{#each $projects as project}
					<label><input type="checkbox" checked={selected.has(project.id)} onchange={() => toggleProject(project.id)} /> {project.name}</label>
				{/each}
				<label><input type="checkbox" checked={outsideProjects} onchange={() => (outsideProjects = !outsideProjects)} /> Outside projects</label>
			</div>
			{#if !hasSelection}<p class="error" role="status">{disabledLine}</p>{/if}
			{#if hasSelection}<p class="impact">{narrowingLine}</p>{/if}
		{/if}
		{#if error}<p class="error" role="alert">{error}</p>{/if}
	</div>
	{#snippet foot()}
		<div class="actions"><Button variant="ghost" onclick={onClose}>Cancel</Button><Button variant="primary" onclick={save} disabled={!hasSelection} loading={saving}>Save</Button></div>
	{/snippet}
</Modal>

<style>
	.where-it-works { display: grid; gap: var(--space-3); }
	.choice { display: flex; gap: var(--space-3); align-items: flex-start; cursor: pointer; color: var(--text-primary); }
	.choice span { display: grid; gap: var(--space-1); }
	.choice small, .impact { color: var(--text-secondary); font-size: var(--text-footnote); line-height: 1.45; margin: 0; }
	.checklist { display: grid; gap: var(--space-2); padding-left: var(--space-6); color: var(--text-secondary); }
	.checklist label { display: flex; gap: var(--space-2); align-items: center; }
	.error { margin: 0; color: var(--red); font-size: var(--text-footnote); }
	.actions { display: flex; justify-content: flex-end; gap: var(--space-2); }
</style>
