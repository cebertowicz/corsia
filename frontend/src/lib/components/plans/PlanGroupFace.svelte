<!--
	The face of a level's Scheduled row: the snooze clock in the waiting circle and a
	title in the row-title style. It never wears the check; that is finished work's.
	The circle and title repeat `PlanRowFace`'s rules (its styles are scoped to it).
-->
<script lang="ts">
	import SnoozeClock from './SnoozeClock.svelte';

	let { count }: { count: number } = $props();
</script>

<span class="state-icon waiting" aria-hidden="true"><SnoozeClock /></span>
<span class="row-copy"><span class="row-line"><span class="row-title">Scheduled · {count}</span></span></span>

<style>
	.state-icon {
		display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px;
		flex: none; border: 1px solid var(--border); border-radius: var(--radius-circle);
		color: var(--text-secondary); font-size: var(--text-caption); font-weight: var(--fw-bold); line-height: 1;
	}
	.row-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; }
	.row-line { display: flex; min-width: 0; align-items: baseline; gap: var(--space-2); }
	.row-title {
		overflow: hidden; min-width: 0; color: var(--text-primary); font-size: var(--text-subhead);
		font-weight: var(--fw-medium); text-overflow: ellipsis; white-space: nowrap;
	}
</style>
