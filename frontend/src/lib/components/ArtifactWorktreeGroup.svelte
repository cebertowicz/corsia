<script lang="ts">
	// A derived presentation group (today: one verified Git worktree) collapsed
	// to a single summary row. The group is never a stored artifact — it exists
	// only for this projection — so it owns disclosure and aggregate status and
	// nothing about artifact lifecycle.
	import type { WorktreeGroupNode } from '$lib/api/artifactPane';
	import ArtifactRow from './ArtifactRow.svelte';

	let { group, expanded = false, onToggle, threadId = null, pendingDisplay = {}, onHide, onPin }: {
		group: WorktreeGroupNode;
		expanded?: boolean;
		onToggle: (key: string) => void;
		threadId?: string | null;
		pendingDisplay?: Record<string, boolean>;
		onHide: (artifactId: string) => void;
		onPin: (artifactId: string, pinned: boolean) => void;
	} = $props();

	// Children are only rendered while open, so aria-controls is only emitted
	// then: pointing it at an absent element would be a dangling IDREF.
	const childrenId = $derived(`artifact-group-${group.key.replace(/[^a-zA-Z0-9_-]/g, '_')}`);
</script>

<button
	type="button"
	class="group-row press-tint"
	aria-expanded={expanded}
	aria-controls={expanded ? childrenId : undefined}
	onclick={() => onToggle(group.key)}
>
	<svg class="chev" class:open={expanded} width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18l6-6-6-6"/></svg>
	<span class="group-label" title={group.label}>{group.label}</span>
	{#if group.attention}<span class="dot attention" aria-hidden="true"></span><span class="sr-only">needs attention</span>{/if}
	<span class="sp"></span>
	<span class="group-count" aria-hidden="true">{group.file_count}</span>
	<span class="sr-only">{group.file_count} {group.file_count === 1 ? 'file' : 'files'}</span>
</button>
{#if expanded}
	<div class="group-children" id={childrenId}>
		{#each group.children as child (child.id)}
			<ArtifactRow item={child} {threadId} pending={!!pendingDisplay[child.id]} {onHide} {onPin} />
		{/each}
	</div>
{/if}

<style>
	.group-row {
		display: flex; align-items: center; gap: 6px; width: 100%; text-align: left;
		padding: 7px 8px; margin-bottom: 1px; border-radius: var(--radius-sm, 8px);
		background: none; border: 0; cursor: pointer; font: inherit; font-size: var(--text-subhead);
		color: var(--text-secondary);
	}
	.group-row:hover { background: var(--bg-hover); }
	.chev { flex-shrink: 0; transition: transform var(--dur-tight) ease; color: var(--text-tertiary); }
	.chev.open { transform: rotate(90deg); }
	.group-label { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
	.sp { flex: 1; }
	.dot.attention { width: 6px; height: 6px; border-radius: var(--radius-circle); background: var(--amber); flex-shrink: 0; }
	.group-count {
		font-size: var(--text-footnote); color: var(--text-tertiary); font-variant-numeric: tabular-nums;
		font-family: var(--font-mono); flex-shrink: 0;
	}
	.group-children { padding-left: 14px; }

	.sr-only {
		position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
		overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
	}
</style>
