<script lang="ts">
	/**
	 * PLANS-AC34 — the one attention dock, in every state that matters.
	 *
	 * Drives the real `AttentionDock` against the real store by stubbing only
	 * `/api/attention/queue`, so what renders here is the production component
	 * and the production state machine, not a mock of either.
	 */
	import { onMount } from 'svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import AttentionDock from '$lib/components/attention/AttentionDock.svelte';
	import { __reset, reload, stale } from '$lib/stores/attentionDock.svelte';
	import { token } from '$lib/stores/auth';
	import { connectionState } from '$lib/stores/connection';
	import type {
		AttentionQueue,
		AttentionQueueItem,
		CommentReplyDetail, CommentReplyItem,
		DecisionDetail,
		ExternalReviewDetail,
	} from '$lib/api/attentionQueue';

	type Scenario =
		| 'empty' | 'one' | 'pick-any' | 'no-recommendation' | 'long-rationale' | 'many' | 'root-direct'
		| 'external' | 'external-many' | 'comment-replies' | 'suppressed' | 'stale';

	const SCENARIOS: Array<{ id: Scenario; label: string }> = [
		{ id: 'empty', label: '0 waiting' },
		{ id: 'one', label: '1 waiting' },
		{ id: 'pick-any', label: 'Pick any' },
		{ id: 'many', label: 'Many waiting' },
		{ id: 'root-direct', label: 'No parent review' },
		{ id: 'external', label: 'External review' },
		{ id: 'external-many', label: 'Review, then decisions' },
		{ id: 'comment-replies', label: 'Comment replies' },
		{ id: 'suppressed', label: 'Host lease holds the slot' },
		{ id: 'stale', label: 'Stale / offline' },
	];

	type Width = 'desktop' | 'mobile-390' | 'mobile-320';
	const WIDTHS: Array<{ id: Width; label: string; px: number | null; height: number | null }> = [
		{ id: 'desktop', label: 'Desktop', px: null, height: null },
		{ id: 'mobile-390', label: '390', px: 390, height: 844 },
		{ id: 'mobile-320', label: '320', px: 320, height: 720 },
	];

	/**
	 * `?frame=attention-dock` renders one dock and nothing else, so the mobile
	 * modes below can load it in a real iframe. A container width cannot fire a
	 * viewport media query; an iframe has its own viewport, so `@media
	 * (max-width: 620px)` is the browser's own answer rather than an assertion.
	 */
	// Read once from the document URL rather than the navigation store: a frame
	// is loaded fresh and never client-navigated, and this keeps the showcase
	// testable without a router mock. `_design` is `ssr = false`, so `window`
	// is always there.
	const frameParams =
		typeof window === 'undefined'
			? new URLSearchParams()
			: new URLSearchParams(window.location.search);
	const framed = frameParams.get('frame') === 'attention-dock';
	const frameScenario = (frameParams.get('scenario') as Scenario | null) ?? 'one';

	let scenario = $state<Scenario>('one');
	let width = $state<Width>('desktop');
	let statusMessage = $state('');

	// Exactly one dock is rendered, at one width at a time. The real shell makes
	// its mount points mutually exclusive via `activeView`; showing two side by
	// side would fabricate a composition the app never produces.
	const selectedWidth = $derived(WIDTHS.find((w) => w.id === width) ?? WIDTHS[0]);
	const viewportPx = $derived(selectedWidth.px);
	const viewportHeight = $derived(selectedWidth.height);

	const source = {
		thread_id: 'child',
		thread_label: 'Reviewer subagent',
		project_id: 'project-corsia',
		project_label: 'CORSIA',
		parent_reviewed: true,
	};

	function decision(over: Partial<DecisionDetail> = {}): DecisionDetail {
		return {
			id: 'decision-1',
			plan_id: 'plan-1',
			node_id: 'node-decision',
			source_node_id: 'node-source',
			thread_id: 'child',
			title: 'How much completed work should the landing canvas retain?',
			context:
				'Completed branches should collapse, but hiding them before the user sees the update makes the rollup feel untrustworthy.',
			tradeoff:
				'More visibility preserves continuity; less visibility keeps the landing surface calm.',
			recommendation: 'A',
			rationale:
				'The passive witness gives the person a clear, reversible receipt at the moment it matters, while avoiding a second acknowledgement step or a persistent history surface that asks them to keep managing already-finished work.',
			options: [
				{ id: 'A', label: 'Seen on canvas open', detail: 'One passive witness, then collapse.' },
				{ id: 'B', label: 'Seen when scrolled into view', detail: 'More precise, more machinery.' },
				{ id: 'C', label: 'Explicitly dismiss', detail: 'Strongest receipt, highest attention tax.' },
			],
			other_enabled: true,
			order_key: 0,
			revision: 3,
			raised_at: '2026-08-02T10:00:00+00:00',
			source: { ...source },
			...over,
		};
	}

	function externalReview(): ExternalReviewDetail {
		return {
			review_key: 'rk-1',
			title: 'Reviewer findings on the plans PR',
			requester: { kind: 'thread', label: 'Reviewer subagent', lane: 'documents' },
			pending_count: 2,
			total_count: 5,
			node_id: 'node-review',
			destination: { version: 1, pane: 'documents', id: 'doc-9' },
			source: { ...source },
		};
	}

	function item(detail: DecisionDetail): AttentionQueueItem {
		return { external_id: detail.id, renderer: 'decision', detail };
	}

	function commentReply(id: string, position: number): CommentReplyItem {
		const detail: CommentReplyDetail = {
			id,
			title: 'A reply arrived on “Clarify the delivery model”',
			unread_count: 1,
			newest_at: `2026-10-24T15:00:0${position}Z`,
			destination: { pane: 'document', id: 'artifact-comment-delivery' },
			source: { thread_id: source.thread_id!, thread_label: source.thread_label },
		};
		return { external_id: id, renderer: 'comment-reply', detail };
	}

	function body(): AttentionQueue {
		if (scenario === 'empty') return { current: null, count: 0, items: [] };
		if (scenario === 'pick-any') {
			const picked = item(
				decision({
					id: 'decision-any',
					title: 'Add these to your agents?',
					tradeoff: 'Ticked items take a few minutes now; unticked ones can be added later.',
					rationale: 'None of them changes anything else on this Mac.',
					pick: 'any',
					recommendation: ['A', 'B'],
					options: [
						{ id: 'A', label: 'Bring over your MCP servers (3)', detail: 'Copies your config.' },
						{ id: 'B', label: 'Web search', detail: 'Needs a search key.' },
						{ id: 'C', label: 'Daily summary', detail: 'One note each morning.' },
					],
				}),
			);
			return { current: picked, count: 1, items: [picked] };
		}
		if (scenario === 'no-recommendation') {
			const unmatched = item(decision({ recommendation: 'Z' }));
			return { current: unmatched, count: 1, items: [unmatched] };
		}
		if (scenario === 'long-rationale') {
			const capped = item(
				decision({
					rationale:
						'The recommended choice keeps the change reversible while making the next action obvious, so the person can decide now without creating another follow-up task or losing the context that explains why this option is a useful way forward today.',
				}),
			);
			return { current: capped, count: 1, items: [capped] };
		}
		if (scenario === 'many') {
			const items = [
				item(decision()),
				item(decision({ id: 'decision-2', title: 'Which provider should answer first?' })),
				item(decision({ id: 'decision-3', title: 'Keep the legacy export path?' })),
				item(decision({ id: 'decision-4', title: 'Ship the migration this week?' })),
			];
			return { current: items[0], count: 4, items };
		}
		if (scenario === 'root-direct') {
			return {
				current: item(decision({ source: { ...source, parent_reviewed: false } })),
				count: 1,
				items: [item(decision({ source: { ...source, parent_reviewed: false } }))],
			};
		}
		if (scenario === 'external') {
			const detail = externalReview();
			return {
				current: { external_id: 'pr-9', renderer: 'external-review', detail },
				count: 1,
				items: [{ external_id: 'pr-9', renderer: 'external-review', detail }],
			};
		}
		if (scenario === 'comment-replies') {
			const items = [commentReply('reply-1', 1), commentReply('reply-2', 2), commentReply('reply-3', 3)];
			return { current: null, count: 0, items: [], reply_items: items };
		}
		if (scenario === 'external-many') {
			const detail = externalReview();
			const items: AttentionQueueItem[] = [
				{ external_id: 'pr-9', renderer: 'external-review', detail },
				item(decision()),
				item(decision({ id: 'decision-2', title: 'Which provider should answer first?' })),
			];
			return { current: items[0], count: items.length, items };
		}
		return { current: item(decision()), count: 1, items: [item(decision())] };
	}

	/** Only the network is stubbed; the component and store are the real ones. */
	function install() {
		const real = globalThis.fetch;
		globalThis.fetch = (async (url: RequestInfo | URL, init?: RequestInit) => {
			if (String(url).includes('/api/attention/queue')) {
				if (scenario === 'stale') throw new TypeError('Failed to fetch');
				const payload = body();
				return new Response(JSON.stringify(payload), {
					status: 200,
					headers: { 'Content-Type': 'application/json' },
				});
			}
			if (String(url).includes('/answer')) {
				// Read back by the e2e spec: the in-page stub is invisible to the network layer.
				(window as unknown as { __showcaseAnswer?: unknown }).__showcaseAnswer = JSON.parse(
					String(init?.body ?? 'null'),
				);
				statusMessage = 'Answer posted (showcase — nothing was persisted).';
				return new Response(JSON.stringify({ decision: { state: 'answered' } }), {
					status: 200,
					headers: { 'Content-Type': 'application/json' },
				});
			}
			return real(url as RequestInfo, init);
		}) as typeof fetch;
		return () => {
			globalThis.fetch = real;
		};
	}

	async function apply(next: Scenario) {
		scenario = next;
		statusMessage = '';
		__reset();
		token.set('design-showcase');
		connectionState.set('connected');
		if (next === 'stale') {
			// Seed a payload first so "stale" shows last-known data, not emptiness.
			scenario = 'one';
			await reload();
			scenario = 'stale';
		}
		await reload();
		stale.set(next === 'stale');
	}

	// Install before child components mount so their first queue read cannot escape
	// to the backend. The route is client-only; teardown restores the real fetch.
	const uninstall = install();

	onMount(() => {
		void apply(framed ? frameScenario : 'one');
		return uninstall;
	});
</script>

{#if framed}
	<div class="frame-only">
		<AttentionDock
			suppressed={frameScenario === 'suppressed'}
			writesDisabled={frameScenario === 'stale'}
		/>
	</div>
{:else}
<div class="showcase">
	<div class="scenario-bar">
		<div>
			<strong>One dock, every state</strong>
			<span>The same decision component used by the thread attention slot.</span>
		</div>
		<div class="scenario-buttons">
			{#each SCENARIOS as option (option.id)}
				<Button
					variant={scenario === option.id ? 'primary' : 'secondary'}
					size="sm"
					onclick={() => apply(option.id)}
				>
					{option.label}
				</Button>
			{/each}
		</div>
	</div>
	{#if statusMessage}<p class="receipt" aria-live="polite">{statusMessage}</p>{/if}

	<div class="width-bar" role="group" aria-label="Viewport width">
		{#each WIDTHS as option (option.id)}
			<Button
				variant={width === option.id ? 'primary' : 'secondary'}
				size="sm"
				onclick={() => (width = option.id)}
			>
				{option.label}
			</Button>
		{/each}
	</div>

	<div class="viewport">
		{#if viewportPx}
			<!-- A real viewport: its own media queries, its own single dock. -->
			<iframe
				title={`Attention dock at ${viewportPx}px`}
				src={`/_design?frame=attention-dock&scenario=${scenario}`}
				style={`width:${viewportPx}px;height:${viewportHeight}px`}
			></iframe>
		{:else}
			<div class="canvas">
				<div class="canvas-filler">Conversation / plan canvas</div>
				<AttentionDock
					suppressed={scenario === 'suppressed'}
					writesDisabled={scenario === 'stale'}
					onOpenDestination={(d) => (statusMessage = `Hand-off to ${d.pane}.`)}
				/>
				<div class="composer-stub">Message CORSIA…</div>
			</div>
		{/if}
	</div>
	<p class="note">
		Empty renders nothing at all. A host-access lease awaiting the user takes the
		slot and the dock stays a disabled one-line bar — deferred, never hidden,
		never stacked.
	</p>
</div>
{/if}

<style>
	.showcase { display: flex; flex-direction: column; gap: var(--space-3); }
	.scenario-bar { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); flex-wrap: wrap; }
	.scenario-bar > div:first-child { display: flex; flex-direction: column; gap: var(--space-1); }
	.scenario-bar strong { color: var(--text-primary); font-size: var(--text-subhead); }
	.scenario-bar span { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.scenario-buttons { display: flex; flex-wrap: wrap; gap: var(--space-2); }
	.receipt { margin: 0; color: var(--text-tertiary); font-size: var(--text-footnote); }
	.width-bar { display: flex; gap: var(--space-2); }
	.viewport { display: flex; }
	.viewport iframe { height: 320px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-primary); }
	.frame-only { display: flex; min-height: 100dvh; flex-direction: column; justify-content: flex-end; padding-bottom: var(--space-4); }
	.canvas { display: flex; width: 100%; max-width: 100%; min-height: 280px; flex-direction: column; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-primary); overflow: hidden; }
	.canvas-filler { flex: 1; display: grid; place-items: center; color: var(--text-tertiary); font-size: var(--text-footnote); }
	.composer-stub { margin: var(--space-2) var(--space-4) var(--space-4); padding: var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-lg); color: var(--text-tertiary); font-size: var(--text-footnote); }
	.note { margin: 0; color: var(--text-tertiary); font-size: var(--text-footnote); }
</style>
