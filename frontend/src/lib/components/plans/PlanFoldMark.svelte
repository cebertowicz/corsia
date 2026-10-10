<!-- The face of a level's fold row: the level's quiet summary, the done mark outlined. -->
<script lang="ts">
	import type { OperatorNode } from '$lib/api/plans';
	import { foldAllCompleted, foldLabel } from './planTreeFold';

	let { folded }: { folded: OperatorNode[] } = $props();
</script>

<span class="fold-mark" class:mixed={!foldAllCompleted(folded)} aria-hidden="true">✓</span>
<span class="fold-label">{foldLabel(folded)}</span>

<style>
	.fold-mark {
		display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px;
		flex: none; border: 1px solid var(--green); border-radius: var(--radius-circle);
		color: var(--green); font-size: var(--text-caption); font-weight: var(--fw-bold); line-height: 1;
	}
	/* A level whose finished work includes a failure or a closure never wears green. */
	.fold-mark.mixed { border-color: var(--text-tertiary); color: var(--text-tertiary); }
	.fold-label {
		min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
		color: var(--text-secondary); font-size: var(--text-footnote); font-variant-numeric: tabular-nums;
	}
</style>
