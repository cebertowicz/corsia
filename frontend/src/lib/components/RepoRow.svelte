<script lang="ts">
	import Toggle from './ui/Toggle.svelte';

	export type Repo = {
		name: string;
		path: string;
		kind: 'linked' | 'native';
		missing: boolean;
		git: { is_repo: boolean; branch: string | null };
		registered: boolean;
		pinned_branch: string | null;
		protect_base_checkout: boolean;
	};

	export type BranchChoice = {
		name: string;
		source: 'local' | 'remote';
		remote: string | null;
	};

	export type BranchCatalog = {
		current_branch: string | null;
		branches: BranchChoice[];
	};

	let {
		repo,
		mutationBusy,
		branchLoading,
		branchCatalog,
		onDetach,
		onLoadBranches,
		onChangeBranch,
		onSetPinned,
		onProtectChange,
	}: {
		repo: Repo;
		mutationBusy: boolean;
		branchLoading: boolean;
		branchCatalog: BranchCatalog | undefined;
		onDetach: () => void;
		onLoadBranches: () => void;
		onChangeBranch: (select: HTMLSelectElement) => void;
		onSetPinned: (next: boolean) => void;
		onProtectChange: (next: boolean) => void;
	} = $props();

	function branchChoices(): BranchChoice[] {
		const choices = branchCatalog?.branches ?? [];
		const selected = repo.pinned_branch ?? repo.git.branch;
		if (!selected || choices.some((choice) => choice.name === selected)) return choices;
		return [{ name: selected, source: 'local', remote: null }, ...choices];
	}

	function shortPath(p: string): string {
		return p.replace(/^\/(Users|home)\/[^/]+/, '~');
	}
</script>

<div class="repo-item" class:missing={repo.missing}>
	<div class="repo-summary">
		<div class="row-text">
			<span class="nm mono">{repo.name}</span>
			<span class="sub mono" title={repo.path}>{shortPath(repo.path)}</span>
		</div>
		{#if !repo.registered}
			<span class="badge muted">unregistered</span>
		{/if}
		{#if repo.missing}
			<span class="badge danger">missing</span>
		{:else if repo.git.is_repo && repo.git.branch}
			<span class="badge">{repo.git.branch}</span>
		{:else if !repo.git.is_repo}
			<span class="badge muted">not a git repo</span>
		{/if}
		{#if repo.kind === 'native'}
			<span class="badge muted">in project</span>
		{:else if repo.registered && !repo.missing}
			<button class="text-action" title="Detach — the repository itself is untouched" aria-label="Detach {repo.name}" onclick={onDetach}>Detach</button>
		{/if}
	</div>

	{#if repo.registered && repo.git.is_repo && !repo.missing}
		<div class="repo-settings">
			<div class="repo-setting">
				<div class="setting-copy">
					<span class="setting-title">Pin branch</span>
					<span class="setting-detail">
						{repo.pinned_branch ? `Pinned to ${repo.pinned_branch}` : 'Agents may switch branches'}
					</span>
				</div>
				{#if repo.pinned_branch}
					{#if branchCatalog}
						<select
							class="branch-select mono"
							aria-label={`Pinned branch for ${repo.name}`}
							value={repo.pinned_branch ?? repo.git.branch ?? ''}
							disabled={mutationBusy}
							onchange={(event) => onChangeBranch(event.currentTarget as HTMLSelectElement)}
						>
							{#each branchChoices() as branch (branch.name)}
								<option value={branch.name}>
									{branch.name}{branch.source === 'remote' && branch.remote
										? ` (${branch.remote})`
										: ''}
								</option>
							{/each}
						</select>
					{:else}
						<button
							class="branch-button"
							aria-label={`Choose branch for ${repo.name}`}
							disabled={mutationBusy || branchLoading}
							onclick={onLoadBranches}
						>
							{branchLoading ? 'Loading…' : 'Change branch'}
						</button>
					{/if}
				{/if}
				<Toggle
					checked={repo.pinned_branch !== null}
					onchange={onSetPinned}
					ariaLabel={`Pin ${repo.name} to a branch`}
					disabled={mutationBusy || branchLoading}
					size="sm"
				/>
			</div>

			<div class="repo-setting">
				<div class="setting-copy">
					<span class="setting-title">Protect base checkout</span>
					<span class="setting-detail">Require worktrees for tracked changes</span>
				</div>
				<Toggle
					checked={repo.protect_base_checkout}
					onchange={onProtectChange}
					ariaLabel={`Protect ${repo.name} base checkout`}
					disabled={mutationBusy}
					size="sm"
				/>
			</div>
		</div>
	{/if}
</div>

<style>
	.repo-item { display: flex; flex-direction: column; gap: var(--space-1); padding: var(--space-1) var(--space-2); }
	.repo-item.missing { opacity: 0.75; }
	.repo-summary { display: flex; align-items: center; gap: var(--space-3); min-width: 0; min-height: var(--list-row-min-height); }
	.row-text { display: flex; flex-direction: column; gap: var(--space-1); flex: 1; min-width: 0; }
	.nm { font-size: var(--text-subhead); color: var(--text-primary); }
	.sub { font-size: var(--text-caption); color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.mono { font-family: var(--font-mono, monospace); }
	/* §0.1 trailing detail: plain caption text, coloured only when it states a failure. */
	.badge { font-size: var(--text-caption); font-family: var(--font-mono, monospace); color: var(--text-tertiary); flex-shrink: 0; }
	.badge.danger { color: var(--red-text); }
	.text-action { background: none; border: 0; padding: 0; cursor: pointer; font: inherit; font-size: var(--text-caption); color: var(--text-tertiary); flex-shrink: 0; }
	.text-action:hover { color: var(--text-primary); }
	.repo-settings { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-2) var(--space-4); }
	.repo-setting { display: flex; align-items: center; gap: var(--space-2); min-width: 0; }
	.setting-copy { display: flex; flex-direction: column; gap: var(--space-1); flex: 1; min-width: 0; }
	.setting-title { font-size: var(--text-footnote); color: var(--text-secondary); }
	.setting-detail { font-size: var(--text-caption); color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.branch-button { background: none; border: none; padding: 2px 3px; cursor: pointer; color: var(--accent-text); font: inherit; font-size: var(--text-caption); white-space: nowrap; }
	.branch-button:disabled { opacity: 0.5; cursor: not-allowed; }
	.branch-select { min-width: 0; max-width: 120px; padding: 3px 5px; border: 1px solid var(--border); border-radius: var(--radius-xs); background: var(--bg-secondary); color: var(--text-primary); font-size: var(--text-caption); }
	@media (max-width: 640px) { .repo-summary { flex-wrap: wrap; } .repo-settings { grid-template-columns: 1fr; } }
</style>
