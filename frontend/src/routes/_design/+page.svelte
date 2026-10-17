<script lang="ts">
	import { Button, Field, IconButton, Message, Modal, Section, StatePill } from '$lib/components/ui';
	import MaterialShowcase from './MaterialShowcase.svelte'; import CommentsShowcase from './CommentsShowcase.svelte'; import SpinnerShowcase from './SpinnerShowcase.svelte';
	import AttentionDockShowcase from './AttentionDockShowcase.svelte'; import SetupVariantsShowcase from './SetupVariantsShowcase.svelte';

	/**
	 * `?frame=attention-dock` renders that showcase and nothing else, so its own
	 * mobile modes can load this route in a real iframe with its own viewport —
	 * a container width cannot fire a viewport media query. The showcase owns
	 * the framed content; this route owns whether anything else is on the page.
	 * Read once from the document URL: a frame is loaded fresh and never
	 * client-navigated, and `_design` is `ssr = false`.
	 */
	const framed =
		typeof window !== 'undefined' &&
		new URLSearchParams(window.location.search).get('frame') === 'attention-dock';
	import AttentionRollup from '$lib/components/AttentionRollup.svelte';
	import ContextSheet from '$lib/components/ContextSheet.svelte';
	import ThreadContextPanel from '$lib/components/ThreadContextPanel.svelte';
	import { activeThreadId, threads } from '$lib/stores/threads';
	import type { AttentionItem } from '$lib/api/attention';
	import type { ContextUsage } from '$lib/stores/threads';

	// Demo items for the attention section
	const demoRaised: AttentionItem[] = [
		{
			external_id: 'demo-1',
			integration: 'github',
			type: 'pull-request',
			subtype: 'review-request',
			attention_state: 'raised',
			requested_action: 'Review and approve PR #42: Add attention surface to coordination MVP',
			urgency: 'high',
			confidence: '0.92',
			owner_thread: 'IMPL · Coordination MVP S6b',
			levels: []
		},
		{
			external_id: 'demo-2',
			integration: 'plan',
			type: 'milestone',
			subtype: null,
			attention_state: 'raised',
			requested_action: 'Confirm sprint goal for coordination-model v2 launch.',
			urgency: 'medium',
			owner_thread: 'DESIGN · Plans & Roadmap',
			levels: []
		},
		{
			external_id: 'demo-3',
			integration: 'plan',
			type: 'task',
			subtype: null,
			attention_state: 'deferred',
			requested_action: 'Approve the Event Registry PRD final draft.',
			urgency: 'low',
			defer_condition: { type: 'deadline', value: '2026-07-01T09:00' },
			owner_thread: 'DESIGN · Event Registry',
			levels: []
		}
	];

	const demoOpenContextSource = () => {};
	const demoContextUsage: ContextUsage = {
		model_context_window: 128000,
		system_tokens: 4200,
		tool_def_tokens: 8700,
		history_tokens: 23300,
		user_tokens: 260,
		total_used: 36460,
		utilization: 0.285,
		preview: true,
		was_trimmed: false,
		history_messages: 34,
		snapshot_id: 'ctx-demo-rc0',
		continuity_mode: 'reconstructed',
		source_artifacts: [
			{
				kind: 'provider-bound-payload',
				harness: 'claude-code',
				method: 'cc-user-frame',
				snapshot_id: 'ctx-demo-rc0',
				content_hash: '1234567890abcdef',
				payload: { frame_text: 'User message:\nship the RC-0 proof' },
			},
			{
				kind: 'provider-bound-payload-preview',
				harness: 'codex',
				method: 'next-turn-preview',
				snapshot_id: 'ctx-demo-rc0',
				content_hash: 'fedcba0987654321',
				payload: { preview: true },
			},
		],
		context_snapshot: {
			snapshot_id: 'ctx-demo-rc0',
			content_hash: 'abcdef1234567890',
			delivery_leaves: [
				{ harness: 'claude-code', destination_mode: 'user-frame' },
			],
			source_inventory: [
				{
					source_id: 'system-bootstrap:GLOBAL.md',
					origin_section: 'workspace',
					label: 'System bootstrap: GLOBAL.md',
					stable_reference: '/Users/alice/Documents/GLOBAL.md',
					freshness: 'fresh',
					inclusion: 'included',
					reason: 'system bootstrap file',
					owner_surface: 'document-viewer',
					prompt_tokens: 6200,
				},
					{
						source_id: 'project-bootloader:corsia:PROJECT.md',
						origin_section: 'project',
						label: 'Project bootloader: PROJECT.md',
						stable_reference: '/Users/alice/Documents/projects/corsia/PROJECT.md',
						freshness: 'fresh',
						inclusion: 'included',
						reason: 'project bootloader file',
						owner_surface: 'document-viewer',
						prompt_tokens: 3100,
						metadata: { project_id: 'corsia' },
					},
					{
						source_id: 'dynamic:skills-index',
						origin_section: 'dynamic',
						label: 'Skills index',
						stable_reference: 'skills://effective/demo-thread',
						freshness: 'fresh',
						inclusion: 'included',
						reason: 'effective scoped skills index',
						owner_surface: 'thread-settings',
						prompt_tokens: 180,
						metadata: {
							provider: 'skills',
							skill_count: 3,
							omitted_count: 2,
							error_count: 1,
							project_id: 'corsia',
						},
					},
					{
						source_id: 'skill:global:review-helper',
						origin_section: 'dynamic',
						label: 'review-helper',
						stable_reference: '/Users/alice/Documents/skills/review-helper/SKILL.md',
						freshness: 'fresh',
						inclusion: 'omitted',
						reason: 'overridden by thread skill with the same name',
						owner_surface: 'system-settings',
						prompt_tokens: null,
						metadata: {
							provider: 'skills',
							scope: 'global',
						},
					},
					{
						source_id: 'skill:thread:broken-skill',
						origin_section: 'dynamic',
						label: 'broken-skill',
						stable_reference: '/Users/alice/Documents/projects/corsia/working/demo-thread/skills/broken-skill/SKILL.md',
						freshness: 'error',
						inclusion: 'omitted',
						reason: 'description is required',
						owner_surface: 'thread-settings',
						prompt_tokens: null,
						metadata: {
							provider: 'skills',
							scope: 'thread',
						},
					},
					{
						source_id: 'dynamic:agent-roster',
						origin_section: 'dynamic',
						label: 'Agent roster',
					stable_reference: 'thread://demo/agents',
					freshness: 'fresh',
					inclusion: 'included',
					reason: 'registered project Agents',
					owner_surface: 'agents',
					prompt_tokens: 780,
					metadata: {
						provider: 'agent_owner_map',
						agent_count: 3,
					},
				},
				{
					source_id: 'model:claude-code',
					origin_section: 'model',
					label: 'Claude Code model: opus',
					stable_reference: 'model://claude-code/opus',
					freshness: 'fresh',
					inclusion: 'included',
					reason: 'selected harness model',
					owner_surface: 'provider-settings',
					metadata: {
						harness: 'claude-code',
						model: 'claude-code/opus',
						execution_class: 'cc_main',
					},
				},
				{
					source_id: 'next-turn-preview',
					origin_section: 'preflight',
					label: 'Codex next-turn preview',
					stable_reference: 'thread://demo/next-turn-preview',
					freshness: 'preview',
					inclusion: 'blocked',
					reason: 'Projected from selected thread runtime before provider dispatch',
					owner_surface: 'context-widget',
					metadata: {
						preflight: {
							status: 'blocked',
							continuity_mode: 'blocked',
							model_context_window: 12000,
							total_tokens: 18800,
						},
					},
				},
			],
		},
	};

	let showSmModal = $state(false);
	let showMdModal = $state(false);
	let showLgModal = $state(false);
	let showAlertModal = $state(false);

	let textValue = $state('hello');
	let numberValue = $state(42);
	let selectValue = $state('a');

	const demoThreadContextThreadId = 'design-thread-context';
	$effect(() => {
		activeThreadId.set(demoThreadContextThreadId);
		threads.set([
			{
				id: demoThreadContextThreadId,
				user_id: 'demo',
				profile: 'default',
				title: 'CORSIA conductor',
				model: 'anthropic/claude-sonnet-4-6',
				project_id: 'corsia',
				bootloader_files: [
					'/Users/alice/Documents/projects/corsia/AGENTS.md',
					'/Users/alice/Documents/projects/corsia/CONTEXT-OWNERSHIP-CUTOVER-DOD.md',
				],
				created_at: '2026-07-02T00:00:00Z',
				updated_at: '2026-07-02T00:00:00Z',
			},
		]);
	});
</script>

<svelte:head>
	<title>Design system</title>
</svelte:head>

{#if framed}
	<AttentionDockShowcase />
{:else}
<div class="page">
	<header class="page-head">
		<div>
			<h1>Design system</h1>
			<p class="lede">
				Primitives in <code>src/lib/components/ui/</code>. Tweak one
				and every consumer updates.
			</p>
		</div>
		<Button variant="secondary">View source</Button>
	</header>

	<section class="block">
		<header class="block-head">
			<h2>Button</h2>
			<p>Default neutral, hovers to violet tint. Primary locks tint on. No gradients.</p>
		</header>
		<div class="card">
			<div class="row">
				<Button variant="primary">Primary</Button>
				<Button variant="secondary">Secondary</Button>
				<Button variant="danger">Danger</Button>
				<Button variant="ghost">Ghost</Button>
			</div>
			<div class="row">
				<Button variant="primary" size="sm">Primary sm</Button>
				<Button variant="secondary" size="sm">Secondary sm</Button>
				<Button variant="danger" size="sm">Danger sm</Button>
				<Button variant="ghost" size="sm">Ghost sm</Button>
			</div>
			<div class="row">
				<Button variant="primary" disabled>Disabled</Button>
				<Button variant="primary" loading>Loading</Button>
			</div>
		</div>
	</section>

	<section class="block">
		<header class="block-head">
			<h2>IconButton</h2>
			<p>Icon-only buttons for close, info, delete. ariaLabel required.</p>
		</header>
		<div class="card">
			<div class="row">
				<IconButton ariaLabel="Close" variant="default">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6 18 18M18 6 6 18"/></svg>
				</IconButton>
				<IconButton ariaLabel="Info" variant="ghost">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="10"/><path d="M12 8v.01M11 12h1v4h1"/></svg>
				</IconButton>
				<IconButton ariaLabel="Delete" variant="danger">
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
				</IconButton>
				<IconButton ariaLabel="Close small" size="sm">
					<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6 18 18M18 6 6 18"/></svg>
				</IconButton>
			</div>
			<div class="row" style="gap: 12px; margin-top: 8px; align-items: center;">
				<span style="font-size:var(--text-footnote);color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.06em;min-width:80px">lg (36/44)</span>
				<IconButton ariaLabel="Close large" size="lg" variant="default">
					<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 6 18 18M18 6 6 18"/></svg>
				</IconButton>
			</div>
			<div class="row" style="gap: 12px; margin-top: 8px; align-items: center; padding: 8px; background: var(--glass-thin); border-radius: 12px;">
				<span style="font-size:var(--text-footnote);color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.06em;min-width:80px">shell (header chrome)</span>
				<IconButton ariaLabel="Toggle sidebar" size="lg" variant="shell">
					<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
						<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="9" y1="4" x2="9" y2="20"/>
					</svg>
				</IconButton>
				<IconButton ariaLabel="New chat" size="lg" variant="shell">
					<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
						<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z"/>
					</svg>
				</IconButton>
			</div>
		</div>
	</section>

	<section class="block">
		<header class="block-head">
			<h2>Field</h2>
			<p>Label + slotted control. Row layout on desktop, stacks on mobile.</p>
		</header>
		<div class="card form">
			<Field label="Text input">
				<input type="text" bind:value={textValue} />
			</Field>
			<Field label="Number with hint" hint="Between 0 and 100.">
				<input type="number" bind:value={numberValue} min="0" max="100" />
			</Field>
			<Field label="Select" required>
				<select bind:value={selectValue}>
					<option value="a">Option A</option>
					<option value="b">Option B</option>
					<option value="c">Option C</option>
				</select>
			</Field>
			<Field label="With error" error="This value is invalid.">
				<input type="text" value="bad input" />
			</Field>
			<Field label="With trailing">
				{#snippet trailing()}
					<span class="t-hint">leave blank for default</span>
				{/snippet}
				<input type="text" placeholder="optional" />
			</Field>
		</div>
	</section>

	<section class="block">
		<header class="block-head">
			<h2>StatePill</h2>
			<p>State-mapped colored pill. Same color tokens as provider rows.</p>
		</header>
		<div class="card">
			<div class="row">
				<StatePill state="loaded" />
				<StatePill state="loading" />
				<StatePill state="idle" />
				<StatePill state="error" />
				<StatePill state="available" />
				<StatePill state="default" />
				<StatePill state="active" />
				<StatePill state="success" />
				<StatePill state="warning" />
			</div>
			<div class="row">
				<StatePill state="loaded" size="sm" />
				<StatePill state="error" size="sm" />
				<StatePill state="loading" size="sm" label="Custom label" />
			</div>
		</div>
	</section>

	<section class="block">
		<header class="block-head">
			<h2>Message</h2>
			<p>Inline status banner — pairs with form actions and modal bodies.</p>
		</header>
		<div class="card stack">
			<Message variant="ok">Saved successfully.</Message>
			<Message variant="err">Connection failed — retry?</Message>
			<Message variant="info">Heads up: this action affects all threads.</Message>
		</div>
	</section>

	<SetupVariantsShowcase />
	<MaterialShowcase />
	<CommentsShowcase /><SpinnerShowcase />
	<!-- ── The one attention dock (PLANS-AC34) ── -->
	<section class="block" id="attention-dock">
		<header class="block-head">
			<h2>Attention dock — the one thing that needs you</h2>
			<p>One item at a time at the work-canvas bottom, in the slot the host-access lease already proved. Plans point at it; they never render it.</p>
		</header>
		<AttentionDockShowcase />
	</section>


	<section class="block">
		<header class="block-head">
			<h2>Attention Rollup — parent thread roll-up</h2>
			<p>Collapsed by default. Expands to show child thread items. No badge/wake. Pull-only.</p>
		</header>
		<div class="card stack">
			<AttentionRollup parentThreadId="demo-parent" demo={demoRaised.filter(i => i.attention_state === 'raised')} />
		</div>
	</section>

	<section class="block">
		<header class="block-head">
			<h2>CTX Sheet — loaded source proof</h2>
			<p>Fully loaded RC-0 source inventory with collapsed categories and one-line prompt/context rows.</p>
		</header>
		<div class="card ctx-fixture">
			<ContextSheet usage={demoContextUsage} onOpenSource={demoOpenContextSource} />
		</div>
	</section>

	<section class="block">
		<header class="block-head">
			<h2>Thread settings — context section</h2>
			<p>Thread-level context management as a section inside the settings canvas.</p>
		</header>
		<div class="card settings-section-fixture">
			<ThreadContextPanel threadId={demoThreadContextThreadId} />
		</div>
	</section>

	<section class="block">
		<header class="block-head">
			<h2>Modal</h2>
			<p>Centered dialog with backdrop. Escape closes. Backdrop click closes.</p>
		</header>
		<div class="card">
			<div class="row">
				<Button variant="primary" onclick={() => showSmModal = true}>Small (480px)</Button>
				<Button variant="secondary" onclick={() => showMdModal = true}>Medium (640px)</Button>
				<Button variant="secondary" onclick={() => showLgModal = true}>Large (800px)</Button>
				<Button variant="danger" onclick={() => showAlertModal = true}>Alert dialog</Button>
			</div>
		</div>
	</section>
</div>
{/if}

{#if showSmModal}
	<Modal title="Small modal" size="sm" onClose={() => showSmModal = false}>
		<p style="margin: 0 0 14px; font-size: var(--text-subhead); color: var(--text-secondary);">
			Body of an sm modal — 480px max-width.
		</p>
		<Field label="Example field">
			<input type="text" placeholder="type here" />
		</Field>
		{#snippet foot()}
			<Button variant="ghost" onclick={() => showSmModal = false}>Cancel</Button>
			<span style="flex: 1"></span>
			<Button variant="primary" onclick={() => showSmModal = false}>Save</Button>
		{/snippet}
	</Modal>
{/if}

{#if showMdModal}
	<Modal title="Medium modal" onClose={() => showMdModal = false}>
		<Message variant="info">An md modal — 640px. Try Escape, or click the backdrop.</Message>
		<Section title="Form">
			<Field label="Name"><input type="text" /></Field>
			<Field label="Email" hint="We won't share it."><input type="email" /></Field>
			<Field label="Access">
				<select>
					<option>Admin</option><option>Member</option>
				</select>
			</Field>
		</Section>
		{#snippet foot()}
			<Button variant="ghost" onclick={() => showMdModal = false}>Cancel</Button>
			<span style="flex: 1"></span>
			<Button variant="ghost" onclick={() => showMdModal = false}>Test</Button>
			<Button variant="primary" onclick={() => showMdModal = false}>Save changes</Button>
		{/snippet}
	</Modal>
{/if}

{#if showLgModal}
	<Modal title="Large modal" size="lg" onClose={() => showLgModal = false}>
		<p style="margin: 0; font-size: var(--text-subhead); color: var(--text-secondary);">
			800px. Useful for tabbed settings or wider content.
		</p>
	</Modal>
{/if}

{#if showAlertModal}
	<Modal
		title="Memory tight"
		size="sm"
		role="alertdialog"
		onClose={() => showAlertModal = false}
	>
		<Message variant="err">Loading this model would push free memory below the safe reserve.</Message>
		<p style="margin: 0; font-size: var(--text-subhead); color: var(--text-secondary);">
			macOS may swap or kill processes if memory drops too low.
		</p>
		{#snippet foot()}
			<Button variant="ghost" onclick={() => showAlertModal = false}>Cancel</Button>
			<span style="flex: 1"></span>
			<Button variant="danger" onclick={() => showAlertModal = false}>Load anyway</Button>
		{/snippet}
	</Modal>
{/if}

<style>
	:global(html), :global(body) { overflow: auto !important; }

	.page {
		max-width: 880px;
		margin: 0 auto;
		padding: 48px 32px 96px;
		color: var(--text-primary);
		min-height: 100vh;
	}

	.page-head {
		display: flex; align-items: flex-start; justify-content: space-between;
		gap: 16px;
		padding-bottom: 28px;
		margin-bottom: 36px;
		border-bottom: 1px solid var(--border-subtle);
	}
	.page-head h1 {
		font-size: var(--text-title2); font-weight: 600;
		letter-spacing: -0.015em;
		color: var(--text-primary);
		margin: 0 0 4px;
	}
	.lede {
		color: var(--text-tertiary);
		margin: 0;
		font-size: var(--text-subhead);
		line-height: 1.5;
	}
	.lede code {
		font-family: 'SF Mono', monospace; font-size: var(--text-footnote);
		background: var(--bg-tertiary);
		padding: 2px 6px;
		border-radius: var(--radius-xs);
		color: var(--text-secondary);
	}

	.block {
		margin-bottom: 32px;
	}
	.block-head {
		margin-bottom: 12px;
	}
	.block-head h2 {
		font-size: var(--text-body); font-weight: 600;
		letter-spacing: -0.01em;
		color: var(--text-primary);
		margin: 0 0 4px;
	}
	.block-head p {
		font-size: var(--text-subhead);
		color: var(--text-tertiary);
		margin: 0;
		line-height: 1.5;
	}

	.card {
		background: var(--bg-secondary);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius);
		padding: 18px 22px;
	}
	.card.settings-section-fixture {
		box-sizing: border-box;
		max-width: 600px;
		padding: 16px 18px;
	}
	.card.form { padding: 4px 22px; }
	.card.stack > :global(*) + :global(*) { margin-top: 8px; }
	.row {
		display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
	}
	.row + .row { margin-top: 10px; }

	.t-hint { font-size: var(--text-footnote); color: var(--text-tertiary); }

</style>
