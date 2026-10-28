<!--
	The status mark of one plan row: a small circle with the state's own glyph ("!" for needs-you).

	Lifted out of PlanRowFace so every surface that says "this needs you" (the plan row, the
	person-step chip in the transcript) draws the one mark.
-->
<script lang="ts">
	import type { Snippet } from 'svelte';

	let { state, tone, label, spinning = false, children }: { state: string; tone: string; label?: string; spinning?: boolean; children: Snippet } = $props();
</script>

<span class="state-icon {state} {tone}" class:spinning title={label} aria-hidden="true">{@render children()}</span>

<style>
	.state-icon {
		display: inline-flex; align-items: center; justify-content: center; width: var(--plan-state-icon-size); height: var(--plan-state-icon-size);
		flex: none; border: 1px solid var(--border); border-radius: var(--radius-circle);
		color: var(--text-secondary); font-size: var(--text-caption); font-weight: var(--fw-bold);
		line-height: 1;
	}
	.state-icon.working {
		border-color: var(--status-in-progress); color: var(--status-in-progress);
	}
	.state-icon.working.spinning { border-top-color: transparent; }
	.state-icon.needs-you { border-color: var(--amber); color: var(--amber); }
	.state-icon.waiting { border-color: var(--border); color: var(--text-secondary); }
	.state-icon.finished.failed { border-color: var(--red); color: var(--red); }
	.state-icon.finished.complete { border-color: var(--green); background: var(--green); color: var(--bg-primary); }
	/* Closed is neither success nor failure: quiet, like work that left the plan. */
	.state-icon.finished.closed { border-color: var(--border); color: var(--text-tertiary); }
	/* The one motion mechanism: only an actually-advancing row spins. */
	.state-icon.spinning { animation: plan-spin 1.2s linear infinite; }
	@keyframes plan-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) {
		.state-icon.spinning { animation: none; }
	}
</style>
