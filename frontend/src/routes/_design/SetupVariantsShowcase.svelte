<script lang="ts">
	import { Button, CheckRow, CodeInput, Field, RailRow, RailSection, StatePill } from '$lib/components/ui';

	/**
	 * The setup screens' primitive variants. Proposed, pending Product Experience and Steve: only the setup
	 * screens (`/_design/setup`) use them, and no other surface changes. Each is one rule block in its own
	 * primitive, so a different decision is a one-file change.
	 */
	let code = $state('4F');
</script>

<section class="block" id="setup-variants">
	<header class="block-head">
		<h2>Setup variants (proposed, pending PX + Steve)</h2>
		<p>The chat canvas's own forms as primitive variants: the send button's fill as a pill, the model chip as a chip, the composer chip as a field, the Update mark's dot as a status, and the shell's sidebar rows. Only setup uses them.</p>
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
		<h3>CodeInput and CheckRow</h3>
		<div class="stack">
			<CodeInput label="Code" bind:value={code} />
			<CheckRow label="Keep CORSIA running when the window is closed" checked={false}>
				{#snippet actions()}<Button variant="chip" size="sm">Open Login Items</Button>{/snippet}
			</CheckRow>
			<CheckRow label="Keep CORSIA running when the window is closed" checked={true} />
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
</style>
