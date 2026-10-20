<!-- The face of a level's fold row: the level's quiet summary, the done mark outlined. -->
<script lang="ts">
	import { foldAccessibleLabel, foldHasNoFailures, foldLabel, type FoldCounts } from './planFoldLevels';

	let { counts }: { counts: FoldCounts } = $props();
</script>

<span class="fold-mark" class:mixed={!foldHasNoFailures(counts)} aria-hidden="true">✓</span>
<span class="fold-label" aria-label={foldAccessibleLabel(counts)}><span aria-hidden="true">{foldLabel(counts)}</span></span>

<style>
	.fold-mark {
		display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px;
		flex: none; border: 1px solid var(--green); border-radius: var(--radius-circle);
		color: var(--green); font-size: var(--text-caption); font-weight: var(--fw-bold); line-height: 1;
	}
	/* A level with a seen failure keeps its muted mark; closed work is done. */
	.fold-mark.mixed { border-color: var(--text-tertiary); color: var(--text-tertiary); }
	.fold-label {
		flex: 1 1 0; min-width: 0; overflow: hidden; color: var(--text-secondary);
		font-size: var(--text-footnote); font-variant-numeric: tabular-nums;
		text-overflow: ellipsis; white-space: nowrap;
	}
	/* This component is the fold face, so it keeps its own summary and control together without growing PlanTree. */
	:global(.fold-row) { flex-wrap: nowrap; }
	:global(.fold-row .row-body) { flex: 1 1 0; }
</style>
