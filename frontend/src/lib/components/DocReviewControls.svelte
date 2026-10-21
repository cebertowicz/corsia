<script lang="ts">
	import { reviewStatus } from '$lib/reviewCopy';
	import { onMount, tick, untrack } from 'svelte';
	import { on } from '$lib/ws/events';
	import { andList, createDocumentReviewState } from '$lib/docReviewState.svelte';
	import { agentColor } from '$lib/docPresence';
	import { toasts } from '$lib/stores/toast';
	import Avatar from './ui/Avatar.svelte';
	import ReviewSurface from './reviews/ReviewSurface.svelte';
	let { artifactId, viewingThreadId, targetKey }: { artifactId: string; viewingThreadId: string | null; targetKey: string | null } = $props();
	const model = createDocumentReviewState();
	const uniqueId = $props.id();
	const id = `document-reviews-${uniqueId}`;
	const MAX_AVATARS = 3;
	let open = $state(false);
	let anchor = $state<HTMLElement | null>(null);
	let requestTrigger = $state<HTMLButtonElement>();
	let root = $state<HTMLElement>();
	let narrow = $state(false);
	const shown = $derived(model.requests.slice(0, MAX_AVATARS));
	const overflow = $derived(model.requests.length - shown.length);
	const statusText = (request: (typeof model.requests)[number]) => reviewStatus(request.state, request.reviewer_name, request.created_at, request.reason, request.comment_count);
	const groupLabel = $derived(`Reviewers: ${model.requests.map((request) => `${request.reviewer_name} (${statusText(request)})`).join(', ')}`);
	$effect(() => {
		const id = artifactId, thread = viewingThreadId, key = targetKey;
		untrack(() => {
			if (model.setTarget(id, thread, key)) { open = false; anchor = null; void model.load(); }
		});
	});
	onMount(() => {
		const unsubscribe = on('artifacts_changed', (frame: unknown) => {
			if (frame && typeof frame === 'object' && (frame as { artifact_id?: unknown }).artifact_id === model.artifactId) void model.load();
		});
		const unsubscribePlans = on('plans_changed', () => { void model.load(); });
		return () => { unsubscribe(); unsubscribePlans(); model.setTarget(null, null, null); };
	});
	function measureCanvas(element: HTMLElement) {
		const canvas = element.closest('.doc') ?? element.closest('.document-header') ?? element;
		const observer = new ResizeObserver((entries) => { narrow = entries[0].contentRect.width <= 600; });
		observer.observe(canvas);
		return { destroy() { observer.disconnect(); } };
	}
	function show(trigger: HTMLElement) { anchor = trigger; open = true; }
	function restoreFocus() {
		// Capture the fallback before closing the last request removes this control's root.
		const fallback = requestTrigger ?? root?.closest('.document-tools')?.querySelector<HTMLButtonElement>('.comment-status');
		const trigger = anchor;
		void tick().then(() => (trigger?.isConnected ? trigger : fallback)?.focus());
	}
	function sent() {
		const reviewers = model.lastSent;
		open = false;
		restoreFocus();
		if (reviewers.length) toasts.show(`Review requested from ${andList(reviewers)}`, 'success');
	}
</script>

{#if viewingThreadId || model.requests.length || open}
<span class="review-controls" bind:this={root} use:measureCanvas>
	{#if model.requests.length}
		<button type="button" class="doc-tool reviewer-group" aria-label={groupLabel} aria-expanded={open} aria-controls={id} onclick={(event) => show(event.currentTarget)}>
			<span class="stack">
				{#each shown as request (request.id)}
					<span class="reviewer" title={`${request.reviewer_name}: ${statusText(request)}`}>
						<Avatar name={request.reviewer_name} color={agentColor(request.reviewer_thread_id)} hidden />
						<span class="mark" class:submitted={request.state === 'submitted'} aria-hidden="true"></span>
					</span>
				{/each}
				{#if overflow > 0}<span class="more" title={model.requests.slice(MAX_AVATARS).map((request) => request.reviewer_name).join(', ')}>+{overflow}</span>{/if}
			</span>
		</button>
	{/if}
	{#if viewingThreadId}
		<button type="button" class="doc-tool review-request" bind:this={requestTrigger} aria-label="Request review" aria-expanded={open} aria-controls={id} title="Request review"
			onclick={(event) => show(event.currentTarget)}>
			<svg aria-hidden="true" viewBox="0 0 16 16"><circle cx="6.5" cy="5.5" r="2.5" /><path d="M2 13.5c0-2.4 2-4 4.5-4s4.5 1.6 4.5 4M13 4v4M11 6h4" /></svg>
			<span class="label">Request review</span>
		</button>
	{/if}
	{#if open}
		<ReviewSurface {model} {id} {anchor} {narrow} canRequest={!!viewingThreadId}
			onSent={sent} onHide={() => open = false} {restoreFocus} />
	{/if}
</span>
{/if}

<style>
	.review-controls { display: contents; }
	.review-request svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; flex: none; }
	.stack { display: inline-flex; align-items: center; }
	.reviewer { position: relative; display: inline-flex; margin-left: -5px; }
	.reviewer:first-child { margin-left: 0; }
	.reviewer :global(.avatar) { border: 2px solid var(--bg-primary); }
	.mark { position: absolute; right: -1px; bottom: -1px; width: 8px; height: 8px; box-sizing: border-box; border: 2px solid var(--bg-primary); border-radius: var(--radius-circle); background: var(--amber); }
	.mark.submitted { background: var(--text-tertiary); }
	.more { margin-left: -5px; padding: 1px 5px; border: 2px solid var(--bg-primary); border-radius: var(--radius-pill); background: var(--bg-tertiary); color: var(--text-secondary); font-size: var(--text-caption); font-weight: var(--fw-semibold); }
	@container doc-canvas (max-width: 600px) {
		.review-request .label { display: none; }
	}
</style>
