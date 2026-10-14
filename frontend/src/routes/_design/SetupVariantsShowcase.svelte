<script lang="ts">
	import { Button, Field, ListRow, RailRow, RailSection, StatePill } from '$lib/components/ui';

	/**
	 * The setup screens' approved primitive variants. They are setup-only until Product Experience approves a
	 * shared rollout.
	 */
	type EvidenceStep = 'todo' | 'current' | 'done';
	type EvidenceJob = 'busy-34' | 'busy-68' | 'done' | 'error';

	let evidenceStep = $state<EvidenceStep>('todo');
	let evidenceJob = $state<EvidenceJob>('busy-34');

	function advanceEvidenceStep() {
		evidenceStep = evidenceStep === 'todo' ? 'current' : evidenceStep === 'current' ? 'done' : 'todo';
	}

	function advanceEvidenceJob() {
		evidenceJob = evidenceJob === 'busy-34' ? 'busy-68' : evidenceJob === 'busy-68' ? 'done' : evidenceJob === 'done' ? 'error' : 'busy-34';
	}
</script>

<section class="block" id="setup-variants">
	<header class="block-head">
		<h2>Setup variants</h2>
		<p>Approved setup-only forms: the send button's fill as a pill, the model chip as a chip, the composer chip as a field, the Update mark's dot as a status, and the shell's sidebar rows.</p>
	</header>
	<div class="card">
		<h3>Button: pill, chip</h3>
		<div class="row">
			<Button variant="pill">Pill</Button>
			<Button variant="pill" disabled>Pill disabled</Button>
			<Button variant="chip">Link</Button>
			<Button variant="primary">Primary today</Button>
		</div>
		<h3>Field: chip</h3>
		<div class="stack">
			<Field variant="chip" layout="stacked" label="Name"><input type="text" value="Steve Cebertowicz" /></Field>
			<Field variant="chip" layout="stacked" label="Password" error="Use at least 8 characters."><input type="password" value="short" /></Field>
			<Field variant="chip" layout="stacked" label="API key">
				<select aria-label="Provider"><option>Anthropic</option><option>OpenAI</option></select>
				<input type="text" placeholder="Paste your key" aria-label="API key" />
			</Field>
		</div>
		<h3>StatePill: dot</h3>
		<div class="row">
			<StatePill look="dot" state="success" label="Ready" />
			<StatePill look="dot" state="loading" label="Loading" />
			<StatePill look="dot" state="error" label="Failed" />
			<StatePill look="dot" state="loaded" label="Loaded" />
			<StatePill look="dot" state="idle" label="Idle" />
		</div>
			<h3>RailSection and RailRow</h3>
		<div class="rail">
			<RailSection label="Setup" first>
				<RailRow label="Done" state="done" onclick={() => {}} />
				<RailRow label="Current" state="current" />
				<RailRow label="Not reached" state="todo" />
				<RailRow label="A choice" state="idle" onclick={() => {}} />
			</RailSection>
			<RailSection label="In the background">
				<RailRow label="Qwen 3.8" state="busy" meta="34% · about 18 min left" progress={0.34} />
				<RailRow label="Starting CORSIA" state="done" />
				<RailRow label="Stopping the old CORSIA" state="error" meta="Won’t stop" onclick={() => {}} />
			</RailSection>
		</div>
		<h3>Evidence fixture: long names and state transitions</h3>
		<p class="fixture-note">Interactive test-only fixture for recording the setup primitives under long content and state changes.</p>
		<div class="fixture-controls">
			<Button variant="pill" onclick={advanceEvidenceStep}>Advance step</Button>
			<Button variant="chip" onclick={advanceEvidenceJob}>Advance background job</Button>
		</div>
		<div class="evidence-grid">
			<div class="rail">
				<RailSection label="Long setup step" first>
					<RailRow label="A very long setup step name that must truncate instead of pushing the rail wider" state={evidenceStep} />
				</RailSection>
				<RailSection label="Long background job">
					{#if evidenceJob === 'busy-34'}<RailRow label="A very long background model download name that remains legible inside the narrow rail" state="busy" meta="34% · about 18 min left" progress={0.34} />
					{:else if evidenceJob === 'busy-68'}<RailRow label="A very long background model download name that remains legible inside the narrow rail" state="busy" meta="68% · about 9 min left" progress={0.68} />
					{:else if evidenceJob === 'done'}<RailRow label="A very long background model download name that remains legible inside the narrow rail" state="done" />
					{:else}<RailRow label="A very long background model download name that remains legible inside the narrow rail" state="error" meta="Couldn’t finish" />{/if}
				</RailSection>
			</div>
			<div class="long-list">
				<ListRow name="A very long provider account name that must wrap or truncate without overlapping its ready action" meta={["A deliberately long secondary value for the ListRow fixture"]} truncate>
					{#snippet actions()}<StatePill look="dot" state="success" label="Ready" />{/snippet}
				</ListRow>
			</div>
		</div>
	</div>
</section>

<style>
	.block { margin: 0 0 var(--space-6); }
	.block-head h2 { margin: 0; font-size: var(--text-title3); }
	.block-head p { margin: var(--space-1) 0 var(--space-3); color: var(--text-secondary); font-size: var(--text-subhead); }
	.card { display: flex; flex-direction: column; gap: var(--space-3); }
	h3 { margin: var(--space-2) 0 0; font-size: var(--text-subhead); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	.row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); }
	.stack { display: flex; flex-direction: column; gap: var(--space-3); max-width: calc(var(--space-8) * 9); }
	.rail { width: var(--sidebar-width); background: var(--glass-thick); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); }
	.fixture-note { margin: 0; color: var(--text-tertiary); font-size: var(--text-footnote); }
	.fixture-controls, .evidence-grid { display: flex; flex-wrap: wrap; gap: var(--space-3); align-items: flex-start; }
	.long-list { width: min(100%, calc(var(--space-8) * 10)); padding: var(--space-3); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); }
</style>
