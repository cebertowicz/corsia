<script lang="ts">
	import { Button, RailRow } from '$lib/components/ui';
	import SetupShell, { type SetupJob, type SetupStep } from '$lib/components/SetupShell.svelte';

	type StepState = 'todo' | 'current' | 'done';
	let step = $state<StepState>('todo');
	let jobState = $state<'busy' | 'done' | 'error'>('busy');
	let jobCount = $state(0);
	let removingJobs = $state(false);

	const steps = $derived<SetupStep[]>([{ label: 'Motion step', icon: 'chip', state: step }]);
	const jobs = $derived<SetupJob[]>(Array.from({ length: jobCount }, (_, index) => ({
		label: index === 0 ? 'First background job' : 'Second background job',
		state: index === 0 ? jobState : 'done',
		meta: index === 0 && jobState === 'busy' ? '34%' : jobState === 'error' ? 'Couldn’t finish' : 'Ready',
		progress: index === 0 && jobState === 'busy' ? 0.34 : undefined,
	})));

	function advanceStep() {
		step = step === 'todo' ? 'current' : step === 'current' ? 'done' : 'todo';
	}

	function advanceJobState() {
		jobState = jobState === 'busy' ? 'done' : jobState === 'done' ? 'error' : 'busy';
	}

	function advanceJobCount() {
		if (jobCount === 0) {
			jobCount = 1;
			removingJobs = false;
		} else if (jobCount === 1) {
			jobCount = removingJobs ? 0 : 2;
		} else {
			jobCount = 1;
			removingJobs = true;
		}
	}
</script>

<div class="fixture">
	<div class="controls" aria-label="Setup motion controls">
		<Button variant="pill" onclick={advanceStep}>Advance step</Button>
		<Button variant="chip" onclick={advanceJobState}>Advance background state</Button>
		<Button variant="chip" onclick={advanceJobCount}>Advance background jobs</Button>
		<RailRow label="Pressable rail row" state="idle" onclick={() => {}} />
	</div>
	<SetupShell {steps} {jobs} title="Setup motion fixture" lede="Test-only setup rail and press evidence." primary={{ label: 'Fixture action' }} />
</div>

<style>
	.fixture { min-height: 100dvh; }
	.controls { position: fixed; z-index: 1; top: var(--space-3); right: var(--space-3); display: flex; align-items: center; gap: var(--space-2); padding: var(--space-2); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-primary); }
	.controls :global(.rail-row) { min-width: calc(var(--space-8) * 5); }
</style>
