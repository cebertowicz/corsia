<script lang="ts">
	import { get } from 'svelte/store';
	import PathPicker from './ui/PathPicker.svelte';
	import ConfirmDialog from './ConfirmDialog.svelte';
	import AddRepoModal from './AddRepoModal.svelte';
	import RepoRow, { type BranchCatalog, type Repo } from './RepoRow.svelte';
	import { token } from '$lib/stores/auth';
	import { toasts } from '$lib/stores/toast';
	import { plainResponseError } from '$lib/plainError';

	let { projectId }: { projectId: string } = $props();


	let repos = $state<Repo[]>([]);
	let loading = $state(true);

	let addOpen = $state(false);
	let addMode = $state<'link' | 'create'>('link');
	let addPinCurrent = $state(true);
	let addProtectBaseCheckout = $state(true);
	let path = $state('');
	let name = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	let pickerOpen = $state(false);

	let detachTarget = $state<Repo | null>(null);
	let mutationBusy = $state<Record<string, boolean>>({});
	let branchLoading = $state<Record<string, boolean>>({});
	let branchCatalogs = $state<Record<string, BranchCatalog>>({});
	let projectGeneration = 0;

	const canSubmit = $derived(
		!busy && (addMode === 'link' ? path.trim().length > 0 : name.trim().length > 0)
	);

	$effect(() => {
		const project = projectId;
		if (!project) return;
		const generation = ++projectGeneration;
		repos = [];
		loading = true;
		mutationBusy = {};
		branchLoading = {};
		branchCatalogs = {};
		addOpen = false;
		pickerOpen = false;
		detachTarget = null;
		busy = false;
		void load(project, generation);
	});

	function isCurrent(generation: number): boolean {
		return projectGeneration === generation;
	}

	async function load(project = projectId, generation = projectGeneration) {
		const t = get(token);
		if (!t) {
			if (isCurrent(generation)) loading = false;
			return;
		}
		loading = true;
		try {
			const res = await fetch(`/api/projects/${project}/repos?token=${t}`);
			if (!res.ok) throw new Error(`Couldn't load repos (${res.status})`);
			const nextRepos = ((await res.json()).repos ?? []) as Repo[];
			if (isCurrent(generation)) repos = nextRepos;
		} catch (e) {
			if (isCurrent(generation)) {
				toasts.show(e instanceof Error ? e.message : 'Could not load repositories', 'error');
			}
		} finally {
			if (isCurrent(generation)) loading = false;
		}
	}

	function openAdd() {
		addMode = 'link';
		addPinCurrent = true;
		addProtectBaseCheckout = true;
		path = '';
		name = '';
		error = null;
		addOpen = true;
	}

	async function submitAdd() {
		if (!canSubmit) return;
		const t = get(token);
		if (!t) return;
		const project = projectId;
		const generation = projectGeneration;
		busy = true;
		error = null;
		try {
			const controls: {
				pinned_branch?: null;
				protect_base_checkout?: false;
			} = {};
			if (!addPinCurrent) controls.pinned_branch = null;
			if (!addProtectBaseCheckout) controls.protect_base_checkout = false;
			const body =
				addMode === 'link'
					? { mode: 'link', path: path.trim(), name: name.trim() || null, ...controls }
					: { mode: 'create', name: name.trim(), ...controls };
			const res = await fetch(`/api/projects/${project}/repos?token=${t}`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(body),
			});
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not add that repository. Try again.'));
			if (!isCurrent(generation)) return;
			addOpen = false;
			await load(project, generation);
			if (isCurrent(generation)) {
				toasts.show(
					addMode === 'link' ? 'Repository linked' : 'Repository created',
					'success',
				);
			}
		} catch (e) {
			if (isCurrent(generation)) {
				error = e instanceof Error ? e.message : 'Could not add repository';
			}
		} finally {
			if (isCurrent(generation)) busy = false;
		}
	}

	async function confirmDetach() {
		const repo = detachTarget;
		detachTarget = null;
		const t = get(token);
		if (!repo || !t) return;
		const project = projectId;
		const generation = projectGeneration;
		try {
			const res = await fetch(`/api/projects/${project}/repos/${encodeURIComponent(repo.name)}?token=${t}`, {
				method: 'DELETE',
			});
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not detach that repository. Try again.'));
			if (!isCurrent(generation)) return;
			await load(project, generation);
			if (isCurrent(generation)) toasts.show(`Detached ${repo.name}`, 'success');
		} catch (e) {
			if (isCurrent(generation)) {
				toasts.show(e instanceof Error ? e.message : "Couldn't detach repository", 'error');
			}
		}
	}

	function replaceRepo(updated: Repo) {
		repos = repos.map((item) => (item.name === updated.name ? updated : item));
	}


	async function loadBranches(repo: Repo): Promise<BranchCatalog | null> {
		const t = get(token);
		if (!t || !repo.registered || branchLoading[repo.name]) return null;
		if (branchCatalogs[repo.name]) return branchCatalogs[repo.name];
		const project = projectId;
		const generation = projectGeneration;
		branchLoading[repo.name] = true;
		try {
			const res = await fetch(
				`/api/projects/${project}/repos/${encodeURIComponent(repo.name)}/branches?token=${t}`,
			);
			if (!res.ok) {
				throw new Error(await plainResponseError(res, 'Could not load this repository’s branches. Try again.'));
			}
			const catalog = (await res.json()) as BranchCatalog;
			if (!isCurrent(generation)) return null;
			branchCatalogs = { ...branchCatalogs, [repo.name]: catalog };
			return catalog;
		} catch (e) {
			if (isCurrent(generation)) {
				toasts.show(e instanceof Error ? e.message : 'Could not load branches', 'error');
			}
			return null;
		} finally {
			if (isCurrent(generation)) delete branchLoading[repo.name];
		}
	}

	async function patchRepo(
		repo: Repo,
		patch: { pinned_branch: string | null } | { protect_base_checkout: boolean },
	): Promise<Repo | null> {
		const t = get(token);
		if (!t || !repo.registered || mutationBusy[repo.name]) return null;
		const project = projectId;
		const generation = projectGeneration;
		mutationBusy[repo.name] = true;
		try {
			const res = await fetch(
				`/api/projects/${project}/repos/${encodeURIComponent(repo.name)}?token=${t}`,
				{
					method: 'PATCH',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify(patch),
				},
			);
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not save this repository. Try again.'));
			const updated = ((await res.json()).repo ?? null) as Repo | null;
			if (!updated) throw new Error('Repository update returned no state');
			if (!isCurrent(generation)) return null;
			replaceRepo(updated);
			return updated;
		} catch (e) {
			if (isCurrent(generation)) {
				toasts.show(
					e instanceof Error ? e.message : 'Could not update repository settings',
					'error',
				);
			}
			return null;
		} finally {
			if (isCurrent(generation)) delete mutationBusy[repo.name];
		}
	}

	async function setBranchPinned(repo: Repo, next: boolean) {
		if (!next) {
			await patchRepo(repo, { pinned_branch: null });
			return;
		}
		let branch = repo.git.branch;
		if (!branch) {
			const catalog = await loadBranches(repo);
			if (!catalog) return;
			branch = catalog.current_branch ?? catalog.branches[0]?.name ?? null;
		}
		if (!branch) {
			toasts.show('No current branch is available to pin', 'error');
			return;
		}
		await patchRepo(repo, { pinned_branch: branch });
	}

	async function changePinnedBranch(repo: Repo, select: HTMLSelectElement) {
		const previous = repo.pinned_branch ?? repo.git.branch ?? '';
		const next = select.value;
		const updated = await patchRepo(repo, { pinned_branch: next });
		if (!updated) select.value = previous;
	}


</script>

<section class="sec">
	<div class="sec-cap">Repositories</div>

	{#if loading}
		<div class="hint">Loading…</div>
	{:else if repos.length === 0}
		<div class="hint">No repositories yet — a project doesn't need one.</div>
	{:else}
		{#each repos as r (r.name)}
			<RepoRow
				repo={r}
				mutationBusy={!!mutationBusy[r.name]}
				branchLoading={!!branchLoading[r.name]}
				branchCatalog={branchCatalogs[r.name]}
				onDetach={() => (detachTarget = r)}
				onLoadBranches={() => void loadBranches(r)}
				onChangeBranch={(select) => void changePinnedBranch(r, select)}
				onSetPinned={(next) => void setBranchPinned(r, next)}
				onProtectChange={(next) => void patchRepo(r, { protect_base_checkout: next })}
			/>
		{/each}
	{/if}

	<button class="add" onclick={openAdd}>＋ Add repository</button>
</section>

<AddRepoModal
	open={addOpen}
	mode={addMode}
	pinCurrent={addPinCurrent}
	protectBaseCheckout={addProtectBaseCheckout}
	{path}
	{name}
	{busy}
	{error}
	{canSubmit}
	onModeChange={(mode) => {
		addMode = mode;
		error = null;
	}}
	onPathChange={(next) => (path = next)}
	onNameChange={(next) => (name = next)}
	onPinChange={(next) => (addPinCurrent = next)}
	onProtectChange={(next) => (addProtectBaseCheckout = next)}
	onBrowse={() => (pickerOpen = true)}
	onSubmit={() => void submitAdd()}
	onCancel={() => (addOpen = false)}
/>

{#if pickerOpen}
	<PathPicker
		title="Choose a repository folder"
		mode="dir"
		onSelect={(p) => (path = p)}
		onClose={() => (pickerOpen = false)}
	/>
{/if}

<ConfirmDialog
	open={!!detachTarget}
	title="Detach {detachTarget?.name}?"
	description="Removes it from this project. The repository itself is not touched."
	confirmLabel="Detach"
	onConfirm={confirmDetach}
	onCancel={() => (detachTarget = null)}
/>

<style>
	.sec {
		border: 1px solid var(--border-subtle); border-radius: var(--lg-card-radius);
		background: var(--bg-secondary); padding: 16px 18px;
		display: flex; flex-direction: column; gap: 10px;
	}
	.sec-cap {
		font-size: var(--text-caption); font-weight: 700; text-transform: uppercase;
		letter-spacing: 0.1em; color: var(--text-tertiary);
	}
	.hint { font-size: var(--text-subhead); color: var(--text-tertiary); padding: 2px 0; }
	.add {
		align-self: flex-start; background: none; border: 1px dashed var(--border);
		border-radius: var(--radius-sm); cursor: pointer; color: var(--text-secondary);
		font-size: var(--text-subhead); padding: 6px 12px; font-family: inherit;
	}
	.add:hover { color: var(--accent); border-color: var(--accent); }
</style>
