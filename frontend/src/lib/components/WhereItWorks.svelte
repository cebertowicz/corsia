<script lang="ts">
	import { projects, loadProjects } from '$lib/stores/projects';
	import { Button, Modal } from '$lib/components/ui';
	import { ALL_PROJECTS_REACH, approveConnectedApp, isOnlyCorsia, saveAccountReach, saveConnectedAppReach, type ConnectedAppsRow } from '$lib/api/connectedApps';

	let { app, approve = false, onClose, onSaved }: { app: ConnectedAppsRow; approve?: boolean; onClose: () => void; onSaved: (app: ConnectedAppsRow) => void } = $props();
	const appName = $derived(app.kind === 'account' ? `Google — ${app.address}` : app.name);
	/** Only CORSIA is its own reach; the sheet names it before anything is chosen, and a save always replaces it. */
	const onlyCorsia = $derived(isOnlyCorsia(app.reach));
	let mode = $state<'all' | 'selected'>(app.reach.mode === 'all' ? 'all' : 'selected');
	let selected = $state(new Set(app.reach.project_ids));
	let outsideProjects = $state(app.reach.outside_projects);
	let saving = $state(false);
	let error = $state('');

	void loadProjects();

	const hasSelection = $derived(mode === 'all' || selected.size > 0 || outsideProjects);
	const projectNames = $derived($projects.filter((project) => selected.has(project.id)).map((project) => project.name));
	const selectedNames = $derived([...projectNames, ...(outsideProjects ? ['Outside projects'] : [])]);
	const allNames = $derived([...$projects.map((project) => project.name), 'Outside projects']);
	const wideningLine = $derived(`Agents in ${allNames.join(', ')} and any new project will be able to use ${appName}.`);
	const originalNames = $derived([
		...$projects.filter((project) => app.reach.project_ids.includes(project.id)).map((project) => project.name),
		...(app.reach.outside_projects ? ['Outside projects'] : []),
	]);
	const narrowingLine = $derived.by(() => {
		if (app.reach.mode === 'all') {
			const losing = allNames.filter((name) => !selectedNames.includes(name));
			return `Agents in ${losing.join(', ')} won't be able to use ${appName}. New projects won't get it.`;
		}
		const from = originalNames.join(', ');
		const to = selectedNames.join(', ');
		// The CORSIA agent works outside projects, so it keeps an Only CORSIA account only when Outside projects stays chosen.
		if (app.kind === 'account' && isOnlyCorsia(app.reach)) {
			return outsideProjects
				? `CORSIA and agents in ${to} will be able to use ${app.address}.`
				: `${appName} will move from CORSIA to ${to}, with its sign-in. CORSIA won't be able to use it.`;
		}
		if (originalNames.length === 0) return `Agents in ${to} will be able to use ${appName}.`;
		return app.kind === 'account'
			? `${appName} will move from ${from} to ${to}, with its sign-in. Agents in ${from} won't be able to use it.`
			: `${appName} will move from ${from} to ${to}, with its sign-in and key. Agents in ${from} won't be able to use it.`;
	});
	const disabledLine = $derived(approve ? 'Select at least one project.' : `Select at least one project, or remove ${appName}.`);
	/** The one effect line: what the choice does, or the plain reason the button is off. Never an error colour. */
	const effectLine = $derived(mode === 'all' ? wideningLine : hasSelection ? narrowingLine : disabledLine);

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
				? ALL_PROJECTS_REACH
				: { mode: 'selected' as const, project_ids: [...selected], outside_projects: outsideProjects };
			if (approve && app.kind !== 'account') onSaved(await approveConnectedApp(app, reach));
			else if (app.kind === 'account') onSaved({ ...(await saveAccountReach(app, reach)).row, kind: 'account' });
			else onSaved(await saveConnectedAppReach(app, reach));
		} catch (cause) {
			error = cause instanceof Error ? cause.message : 'Could not update this app. Try again.';
		} finally {
			saving = false;
		}
	}
</script>

<Modal title={`Where ${appName} works`} size="sm" {onClose}>
	<div class="where-it-works">
		{#if onlyCorsia}<p class="second">Today, only CORSIA can use this account.</p>{/if}
		<label class="choice">
			<input type="radio" name="reach" checked={mode === 'all'} onchange={selectAllProjects} />
			<span class="label">All projects</span>
			<span class="second">Includes new projects and Outside projects</span>
		</label>
		<label class="choice">
			<input type="radio" name="reach" checked={mode === 'selected'} onchange={selectIndividually} />
			<span class="label">Select individually</span>
		</label>
		{#if mode === 'selected'}
			<div class="checklist">
				{#each $projects as project}
					<label class="choice"><input type="checkbox" checked={selected.has(project.id)} onchange={() => toggleProject(project.id)} /><span class="label">{project.name}</span></label>
				{/each}
				<label class="choice"><input type="checkbox" checked={outsideProjects} onchange={() => (outsideProjects = !outsideProjects)} /><span class="label">Outside projects</span></label>
			</div>
		{/if}
		<div class="effect">
			<!-- An app that runs on this computer carries the server's notice while it waits; it shows when you approve it (B-R22). -->
			{#if approve && app.kind !== 'account' && app.notice}<p class="second">{app.notice}</p>{/if}
			<p class="second" role="status">{effectLine}</p>
			{#if error}<p class="error" role="alert">{error}</p>{/if}
		</div>
	</div>
	{#snippet foot()}
		<div class="actions"><Button variant="ghost" onclick={onClose}>Cancel</Button><Button variant="primary" onclick={save} disabled={!hasSelection} loading={saving}>{approve ? 'Approve' : 'Save'}</Button></div>
	{/snippet}
</Modal>

<style>
	.where-it-works { display: grid; gap: var(--space-3); }
	.choice { display: grid; grid-template-columns: auto 1fr; column-gap: var(--space-3); align-items: start; cursor: pointer; color: var(--text-primary); }
	.choice input { accent-color: var(--accent); margin: 0; margin-top: calc((1lh - var(--space-4)) / 2); width: var(--space-4); height: var(--space-4); line-height: inherit; }
	.label { font-size: var(--text-body); font-weight: var(--fw-regular); line-height: var(--lh-snug); }
	.choice .second { grid-column: 2; }
	.second { margin: 0; color: var(--text-tertiary); font-size: var(--text-caption); line-height: var(--lh-normal); }
	.checklist { display: grid; gap: var(--space-2); padding-left: var(--space-6); }
	.effect { display: grid; gap: var(--space-2); }
	.error { margin: 0; color: var(--red-text); font-size: var(--text-footnote); }
</style>
