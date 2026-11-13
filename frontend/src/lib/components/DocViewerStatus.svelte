<script lang="ts">
	import DocFeedbackStatus from './DocFeedbackStatus.svelte';
	import DocLiveStatus from './DocLiveStatus.svelte';
	import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
	import type { DocOpenRefusal, LiveUnavailable } from '$lib/docOpenRefusal';
	import DocReviewControls from './DocReviewControls.svelte';
	import CommentsPill from './comments/CommentsPill.svelte';
	import { Button, StatePill, Toggle } from '$lib/components/ui';
	import { formatFreshness, type DocumentFreshness, shouldOfferVerification } from '$lib/docFreshness';
	import type { CommentFeedback } from '$lib/api/docs';
	import type { DocFeedback } from '$lib/docFeedback.svelte';
	import { liveConnectionDescriptionId } from '$lib/liveConnectionDescription';
	import { deliverCommentsImmediately, setDeliverCommentsImmediately } from '$lib/commentDeliveryPreference';

	let {
		title = 'Document',
		presence,
		reviewBar,
		commentsOpen = false,
		commentsLoading = false,
		saveStatus = 'idle',
		ready,
		stale,
		readonly,
		commentCount,
		unreadCount = 0,
		artifactId,
		targetInstanceId,
		viewingThreadId = null,
		reviewTargetKey = null,
		onReload,
		onOpenComments,
		freshness,
		onFeedbackChanged,
		onMarkVerified,
		liveSession = null,
		openRefusal = null,
		liveUnavailable = null,
		tabText = '',
		onReopen,
		onRead,
		imageOpen = false,
		activityRoot = null,
		historyAvailable = false,
		historyOpen = false,
		onOpenHistory,
		resolvedCount = 0,
		resolvedOpen = false,
		onOpenResolved,
	}: {
		title?: string;
		presence?: import('svelte').Snippet;
		/** The review bar sits first in the header, so its buttons come before the document in tab order. */
		reviewBar?: import('svelte').Snippet;
		commentsOpen?: boolean;
		commentsLoading?: boolean;
		saveStatus?: 'idle' | 'saving' | 'saved';
		ready: boolean;
		stale: boolean;
		readonly: boolean;
		commentCount: number;
		unreadCount?: number;
		artifactId: string | null;
		targetInstanceId: string | null;
		viewingThreadId?: string | null;
		reviewTargetKey?: string | null;
		onReload?: () => void;
		onOpenComments: (opener: HTMLElement) => void;
		freshness: DocumentFreshness | null;
		onFeedbackChanged?: () => void | Promise<void>;
		onMarkVerified?: () => Promise<void>;
		liveSession?: LiveDocumentSession | null;
		openRefusal?: DocOpenRefusal | null;
		liveUnavailable?: LiveUnavailable | null;
		tabText?: string;
		onReopen?: () => void;
		onRead?: () => void;
		/** An image of this document covers it; its viewer owns the image lane's Send until it is left. */
		imageOpen?: boolean;
		activityRoot?: HTMLElement | null;
		/** The file has recorded versions (slice 1: instruction files). Without them there is no History, and no count. */
		historyAvailable?: boolean;
		historyOpen?: boolean;
		onOpenHistory?: () => void;
		/** Resolved threads behind the text control that opens their drawer, at every width; zero (none resolved, or an image viewer) hides it. */
		resolvedCount?: number;
		resolvedOpen?: boolean;
		onOpenResolved?: () => void;
	} = $props();

	let feedbackStatus = $state<DocFeedbackStatus>();
	let detailsOpen = $state(false);
	let verifying = $state(false);
	let verificationError = $state('');
	let verificationNotice = $state('');
	// The unsent comments, for the pill's dot and accessible summary; the batch itself is the one source.
	const unsent = $derived(feedbackStatus?.controller().pendingCount ?? 0);
	// Delivery controls mount in the identity row so Send holds the compact header line.
	// A live region only announces a change, so the stale state needs text that appears when it starts.
	const staleAnnouncement = $derived(stale && ready ? 'File changed since you opened it. Reload to see the latest.' : '');
	const freshnessPresentation = $derived(freshness ? formatFreshness(freshness) : null);
	// The live-status row renders the stable connection description for unavailable actions.
	const connectionDescriptionId = $derived(liveConnectionDescriptionId(liveSession?.artifactId));
	$effect(() => {
		artifactId;
		detailsOpen = false;
		verificationError = '';
		verificationNotice = '';
	});

	async function changeImmediateDelivery(value: boolean): Promise<void> {
		if (!(await setDeliverCommentsImmediately(value))) return;
		// Another client may have held a comment this one has not synced yet, so turning delivery on always asks the
		// server to send the open document's held lane. An empty lane delivers nothing and shows nothing.
		if (value) void feedbackStatus?.controller().sendDocument();
	}

	async function markVerified(): Promise<void> {
		if (!onMarkVerified || verifying) return;
		verifying = true;
		verificationError = '';
		try {
			await onMarkVerified();
			verificationNotice = 'Document marked verified.';
		} catch (error) {
			verificationError = error instanceof Error ? error.message : 'Could not mark this document verified.';
		} finally {
			verifying = false;
		}
	}

	function detailsKeys(element: HTMLElement) {
		const onKey = (event: KeyboardEvent) => {
			if (event.key !== 'Escape' || !detailsOpen || !(event.target instanceof Node)) return;
			const toggle = element.querySelector<HTMLButtonElement>('.identity-details');
			if (event.target !== toggle && !element.querySelector('.document-metadata')?.contains(event.target)) return;
			event.preventDefault();
			event.stopPropagation();
			detailsOpen = false;
			toggle?.focus();
		};
		element.addEventListener('keydown', onKey);
		return { destroy() { element.removeEventListener('keydown', onKey); } };
	}

	// The viewer reads the document's feedback once, often before this row's status child exists (it mounts only once
	// there is something to show). The latest read waits here and goes to the matching child when it mounts, so the
	// initial state is never dropped and no second read is needed.
	let buffered: { id: string; feedback: CommentFeedback } | null = null;
	$effect(() => {
		const child = feedbackStatus;
		const current = artifactId;
		if (!buffered) return;
		if (buffered.id !== current) buffered = null;
		else if (child) child.sync(buffered.id, buffered.feedback);
	});

	// An image exit that never completed (a reload or teardown) leaves its lane held with no viewer to send it. The
	// next time this document mounts uncovered, it sends that remainder once, before the lane can stay hidden.
	let recoveredFor: string | null = null;
	$effect(() => {
		const held = feedbackStatus?.controller();
		if (!held || imageOpen || !held.artifactId || held.imageHeld === 0) return;
		if (recoveredFor === held.artifactId) return;
		recoveredFor = held.artifactId;
		void held.sendImage();
	});

	export function commentCreated(): void { feedbackStatus?.commentCreated(); }
	export function sync(id: string, feedback: CommentFeedback): void {
		buffered = { id, feedback };
		feedbackStatus?.sync(id, feedback);
	}
	export function feedback(): DocFeedback | undefined { return feedbackStatus?.controller(); }
</script>

<header class="document-header" use:detailsKeys>
	{#if reviewBar}{@render reviewBar()}{/if}
	<div class="document-identity">
		<button type="button" class="identity-details" aria-label="Document details" aria-expanded={detailsOpen}
			onclick={() => detailsOpen = !detailsOpen}>
			<span class="document-title" title={title}>{title}</span>
			{#if freshness && freshnessPresentation}<span class="identity-age" title={`Owner: ${freshness.owner_name} · ${freshnessPresentation.phrase}`}><span class="identity-owner">Owner: {freshness.owner_name}</span><span class="identity-verified">{freshnessPresentation.phrase}</span></span>{/if}
		</button>
		{#if freshnessPresentation?.overdue}<StatePill state="warning" size="sm" label="Overdue" />{/if}
		{#if presence}{@render presence()}{/if}
		<span class="document-tools document-status-row">
			<!-- Two groups, the only places the cluster may break: what is being sent and counted, then how it is delivered and reviewed. -->
			<span class="tool-group tool-send">
			{#if artifactId}
				<!-- Send belongs to this tool cluster with the comment count, delivery preference, and review action. -->
				<span class="feedback-controls"><DocFeedbackStatus bind:this={feedbackStatus} {artifactId} {targetInstanceId} {onFeedbackChanged} {activityRoot} lane="document-held" /></span>
			{/if}
			{#if stale && ready}<button type="button" class="doc-tool warn stale-control" onclick={() => onReload?.()}>File changed · Reload</button>{/if}
			{#if saveStatus !== 'idle'}<span class="save-ghost" class:saving={saveStatus === 'saving'}>{saveStatus === 'saving' ? 'Saving…' : '✓ Saved'}</span>{/if}
			<CommentsPill class="doc-tool comment-status" count={commentCount} unread={unreadCount} {unsent}
				loading={commentsLoading} unknown={!ready} expanded={commentsOpen} disabled={!ready || commentsLoading} onOpen={onOpenComments} />
			{#if resolvedCount && onOpenResolved}<button type="button" class="doc-tool resolved-tool" data-resolved-entry aria-haspopup="dialog" aria-expanded={resolvedOpen} onclick={onOpenResolved}>Resolved · {resolvedCount}</button>{/if}
			</span>
			<span class="tool-group tool-more">
			{#if artifactId}
				<span class="tool-group tool-deliver">
					<span class="delivery-toggle" title="When off, comments send when you close the document or pause for two minutes.">
						<span>Deliver immediately</span>
						<Toggle size="sm" touch checked={$deliverCommentsImmediately} onchange={changeImmediateDelivery} ariaLabel="Deliver comments immediately" />
					</span>
				</span>
			{/if}
			<span class="tool-group tool-review">
			{#if artifactId}
				<DocReviewControls {artifactId} {viewingThreadId} targetKey={reviewTargetKey ?? targetInstanceId} />
			{/if}
			{#if historyAvailable && onOpenHistory}<button type="button" class="doc-tool history-tool" aria-label="History" title="History" aria-expanded={historyOpen} onclick={onOpenHistory}>
				<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 2.35-5.65L4 8.5M4 4.5v4h4M12 7.5V12l3 2" /></svg>
				<span class="label">History</span>
			</button>{/if}
			</span>
			</span>
		</span>
		<!-- Document-held sender is mounted above, in the identity row. -->
	</div>
{#if freshness && freshnessPresentation}
	<div class="document-metadata" class:expanded={detailsOpen}>
		<div class="metadata-owner">Owner: {freshness.owner_name}</div>
		{#if freshnessPresentation.datetime}
			<time datetime={freshnessPresentation.datetime}>
				{freshnessPresentation.phrase}
				{#if freshnessPresentation.dateText}<span class="sr-only"> ({freshnessPresentation.dateText})</span>{/if}
			</time>
		{:else}
			<span class="metadata-secondary">{freshnessPresentation.phrase}</span>
		{/if}
		{#if freshnessPresentation.overdue}<StatePill state="warning" size="sm" label="Overdue" />{/if}
		{#if shouldOfferVerification(freshness)}
			<span class="verification-action"><Button size="sm" variant="ghost" onclick={markVerified} loading={verifying}>Mark verified</Button></span>
		{/if}
		<!-- Verification recovery is rendered outside the static metadata disclosure. -->
	</div>
{/if}

<DocLiveStatus session={liveSession} {openRefusal} {liveUnavailable} {tabText} {onReopen} {onRead}
	connectionStatusId={connectionDescriptionId} />
<span class="metadata-status" class:sr-only={!verificationError} class:metadata-error={!!verificationError} role="status">{verificationError || verificationNotice || staleAnnouncement}</span>
{#if readonly && ready}
	<div class="readonly-note">Read-only.</div>
{/if}
<!-- Delivery controls above remain mounted with the document identity so they never force a second header row. -->
</header>

<style>
	.document-header { grid-area: header; min-width: 0; flex: none; display: flex; flex-direction: column; gap: 6px; padding-left: var(--doc-inset, 24px); background: var(--bg-primary); }
	.document-identity { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-width: 0; }
	.identity-details { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; border: 0; padding: 2px 0; background: none; color: var(--text-primary); cursor: pointer; text-align: left; }
	.document-title { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--text-subhead); font-weight: var(--fw-semibold); }
	.identity-age { display: none; }
	.feedback-controls { display: contents; }
	.identity-details:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-sm); }
	.document-metadata, .document-status-row { min-width: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
	/* One toolbar: every control is the same quiet ghost button in the app's UI type. */
	.document-status-row { gap: 4px; }
	.document-status-row :global(.doc-tool) { position: relative; display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 28px; padding: 0 8px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); font-family: var(--font-sans); font-size: var(--text-footnote); font-weight: var(--fw-medium); white-space: nowrap; cursor: pointer; }
	.document-status-row :global(.doc-tool:hover:not(:disabled)) { background: var(--bg-tertiary); color: var(--text-primary); }
	.document-status-row :global(.doc-tool:focus-visible) { outline: 2px solid var(--accent); outline-offset: -2px; }
	.document-status-row :global(.doc-tool:disabled) { opacity: 0.5; cursor: not-allowed; }
	.document-status-row :global(.doc-tool.warn) { background: var(--amber-bg); color: var(--amber-text); }
	.document-status-row :global(.doc-tool.warn:hover:not(:disabled)) { background: color-mix(in srgb, var(--amber) 18%, transparent); color: var(--amber-text); }
	.document-status-row :global(.doc-tool[aria-disabled='true']) { opacity: 0.65; cursor: not-allowed; }
	.document-metadata { color: var(--text-secondary); font-size: var(--text-footnote); }
	.metadata-owner { flex: 1 1 10rem; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.verification-action { margin-left: -10px; }
	.metadata-secondary, .document-metadata time { color: var(--text-secondary); }
	.metadata-error { color: var(--red); }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
	/* After the base rules above: equal specificity, so the later rule wins at phone width. */
	@container doc-canvas (max-width: 600px) {
		.identity-details { min-height: 44px; justify-content: center; }
		.identity-age { display: flex; width: 100%; gap: 6px; color: var(--text-secondary); font-size: var(--text-caption); container: identity-age / inline-size; }
		.identity-owner { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		/* The line gets what the tools leave it: the verified phrase truncates inside it, and the owner, which would show as a clipped fragment, steps aside first. */
		.identity-verified { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		@container identity-age (width < 7.5rem) { .identity-owner { display: none; } }
		.document-metadata:not(.expanded) { display: none; }
		.document-status-row { flex-wrap: wrap; }
		.document-status-row :global(.doc-tool) { min-height: 44px; padding: 0 12px; }
		.feedback-controls :global(.undelivered:not(.idle)) { flex: 1 1 100%; }
	}

	/* Slice-one review header: the title is already named by the tab or app bar, so only document tools remain in the slim row. */
	/* Where the plan pane overlays the canvas (PaneShell), the header's content ends before it instead of under it: the tools, both ask lines and the notices.
	   The header is a container, so the ask's line 1 can rearrange when that leaves it too narrow (ReviewBar). */
	.document-header { gap: var(--space-1); border-bottom: 1px solid var(--border-subtle); padding-bottom: var(--space-1); padding-inline-end: var(--pane-overlay-inset, 0px); container: doc-header / inline-size; }
	.document-identity { justify-content: flex-end; }
	.identity-details { display: none; }
	/* The cluster keeps one row while it fits and wraps inside the header when it cannot, whatever it holds: a held sender, a reason, Reload, a long count. */
	.document-tools { position: relative; z-index: 1; flex: none; display: inline-flex; flex-wrap: wrap; max-inline-size: 100%; justify-content: flex-end; align-items: center; gap: var(--space-1); }
	/* The cluster breaks only where content does not fit, in this order: between [Send · count] and the rest; inside the rest, between [Deliver immediately · switch] and [reviewers · Request review · History]; and, only when [Send · count] alone is wider than the header, between Send and the count. The label never leaves its switch, and the reviewers, Request review and History stay on one row. */
	.tool-group { display: inline-flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: var(--space-1); max-inline-size: 100%; }
	.tool-deliver, .tool-review { flex-wrap: nowrap; }
	.tool-review:not(:has(:global(*))) { display: none; }
	/* Send holds the identity line at compact widths; the existing tool cluster is the element that yields and wraps. */
	.feedback-controls { flex: none; min-width: max-content; }
	/* The label sits the chip padding from the switch at every width: the switch's touch size is a hit area inside the switch, so it never moves the label. */
	.delivery-toggle { display: inline-flex; align-items: center; gap: var(--chip-inline-padding); color: var(--text-secondary); font-size: var(--text-footnote); white-space: nowrap; }
	@container doc-header (max-width: 32.5rem) {
		.document-tools, .tool-group { gap: 0; }
	}
	@container doc-canvas (max-width: 37.5rem) {
		.identity-details:has(.identity-age) { display: flex; }
		.document-title { display: none; }
		/* The delivery label is the full "Deliver immediately" at every width; at phone width only the tool padding and the switch's hit area change, and the cluster wraps between its groups when one row cannot hold it. */
		.document-header :global(.doc-tool) { min-height: var(--touch-target); padding: 0 var(--space-2); }
		.delivery-toggle { min-height: var(--touch-target); --toggle-hit: var(--touch-target); }
		/* The tool row cannot shrink, and with Reload, the count and the review controls the save pill pushes it past the left edge: the save state is transient and Reload is not, so the pill yields. */
		.stale-control ~ .save-ghost { display: none; }
	}
	/* When an ask exists, its first line and the tool cluster share one row; the second ask line spans the canvas. */
	.document-header:has(:global(.rb-slot)) { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: start; column-gap: var(--space-1); }
	.document-header:has(:global(.rb-slot)) > :global(*) { grid-column: 1 / -1; }
	.document-header:has(:global(.rb-slot)) > :global(.rb-slot) { grid-column: 1 / -1; grid-row: 1 / 3; }
	.document-header:has(:global(.rb-slot)) > .document-identity { display: contents; }
	.document-header:has(:global(.rb-slot)) .identity-details { grid-column: 1 / -1; grid-row: 3; }
	.document-header:has(:global(.rb-slot)) :global(.presence-row) { grid-column: 2; grid-row: 1; align-self: start; min-height: var(--space-7); position: relative; z-index: 1; }
	.document-header:has(:global(.rb-slot)) .document-tools { grid-column: 3; grid-row: 1; align-self: start; min-height: var(--space-7); position: relative; z-index: 1; }
	.document-header:has(:global(.rb-slot)) :global(.doc-tool) { min-height: var(--space-7); }
	@container doc-header (width < 408px) {
		/* With the review surface in the header, the tool cluster takes the full row under it and wraps between its two groups when one row cannot hold it. */
		.document-header:has(:global(.rb-slot)) .document-tools { grid-column: 1 / -1; min-width: 0; justify-content: flex-end; }
		.document-header:has(:global(.rb-slot)) > :global(.rb-slot) { grid-row: 2 / 3; }
	}
	/* History is a text button on a wide canvas and, like the Request review button beside it (DocReviewControls.svelte:97), an icon-only button once the canvas is phone-sized, so the row holds the same controls whatever the save state. */
	.history-tool svg { display: none; width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 2.1; stroke-linecap: round; stroke-linejoin: round; flex: none; }
	@container doc-canvas (max-width: 600px) {
		.history-tool svg { display: block; }
		.history-tool .label { display: none; }
	}
	/* Wherever the switch has its 44px target the switch's row is that tall, so the target sits inside the row and never over Send, the count or the document: a window up to 768px wide can still have a canvas wider than 600px, where the other tools are 28px. */
	@media (max-width: 768px) {
		.delivery-toggle { min-height: var(--touch-target); --toggle-hit: var(--touch-target); }
	}
	@media (any-pointer: coarse) {
		@container doc-canvas (max-width: 600px) {
			.document-header:has(:global(.rb-slot)) { align-items: center; }
			.document-header:has(:global(.rb-slot)) :global(.doc-tool) { min-height: var(--touch-target); }
		}
	}
</style>
