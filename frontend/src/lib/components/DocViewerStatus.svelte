<script lang="ts">
	import DocFeedbackStatus from './DocFeedbackStatus.svelte';
	import DocLiveStatus from './DocLiveStatus.svelte';
	import DocOwnerControl from './DocOwnerControl.svelte';
	import MiddleName from './MiddleName.svelte';
	import type { DocOwnership } from '$lib/api/docOwnership';
	import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
	import type { DocOpenRefusal, LiveUnavailable } from '$lib/docOpenRefusal';
	import DocReviewControls from './DocReviewControls.svelte';
	import { Button, Toggle } from '$lib/components/ui';
	import { formatFreshness, type DocumentFreshness, shouldOfferVerification } from '$lib/docFreshness';
	import type { CommentFeedback } from '$lib/api/docs';
	import type { DocFeedback } from '$lib/docFeedback.svelte';
	import { liveConnectionDescriptionId } from '$lib/liveConnectionDescription';
	import { deliverCommentsImmediately, setDeliverCommentsImmediately } from '$lib/commentDeliveryPreference';
	import { publishDocumentBar, type DocumentAsk } from '$lib/stores/documentBar';
	import { canvasCompact } from '$lib/stores/artifactCanvas';
	import { isMobile } from '$lib/stores/viewport';
	import { paneOpen } from '$lib/stores/paneOpen';

	let {
		presence,
		reviewBar,
		ask = null,
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
		ownership = null,
		headerStandIn = false,
		onOwnershipChanged,
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
		resolvedInColumn = false,
		onOpenResolved,
		commentsEntry = $bindable(null),
		detailsEntry = $bindable(null),
	}: {
		presence?: import('svelte').Snippet;
		/** The ask's card, docked to the canvas's bottom edge; its buttons come before the document in tab order. */
		reviewBar?: import('svelte').Snippet;
		ask?: DocumentAsk | null;
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
		/** Who owns and supervises a registered document; its row shows both, each with its own picker. */
		ownership?: DocOwnership | null;
		/** The row is the one this tab last showed, drawn before the load answered: its owner pickers and Mark verified wait for the load. */
		headerStandIn?: boolean;
		onOwnershipChanged?: (value: DocOwnership) => void;
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
		/** The resolved list opens in the margin column's place, a plain region rather than a dialog. */
		resolvedInColumn?: boolean;
		onOpenResolved?: () => void;
		/** The Comments button wherever the bar draws it: the list the canvas opens on its own hangs from it. */
		commentsEntry?: HTMLButtonElement | null;
		/** The Details button: what a list or picker opened from inside the closed panel returns to. */
		detailsEntry?: HTMLButtonElement | null;
	} = $props();

	let feedbackStatus = $state<DocFeedbackStatus>();
	let verifying = $state(false);
	let verificationError = $state('');
	let verificationNotice = $state('');
	// A live region only announces a change, so the stale state needs text that appears when it starts.
	const staleAnnouncement = $derived(stale && ready ? 'File changed since you opened it. Reload to see the latest.' : '');
	const freshnessPresentation = $derived(freshness ? formatFreshness(freshness) : null);
	// The live-status row renders the stable connection description for unavailable actions.
	const connectionDescriptionId = $derived(liveConnectionDescriptionId(liveSession?.artifactId));
	/** File changed, until Reload clears it. */
	const urgent = $derived(stale && ready);
	/** The app bar draws File changed after the name while the canvas is too narrow to split; wider, it is a row in Details. */
	const changeInBar = $derived($canvasCompact);
	/** On a phone with the panel closed, the app bar's Toggle panel is here instead, as Show panel. */
	const paneHere = $derived($isMobile && $canvasCompact && !$paneOpen);
	/** What needs you behind Details and is nowhere else on screen: Overdue, File changed where the bar doesn't show it, an ask whose card is hidden. */
	const needsCount = $derived((freshnessPresentation?.overdue ? 1 : 0) + (urgent && !changeInBar ? 1 : 0) + (ask && !ask.onCard ? 1 : 0));
	// The unsent comments, for the Comments button's accessible summary (CommentsPill's naming); the batch itself is the one source.
	const unsent = $derived(feedbackStatus?.controller().pendingCount ?? 0);
	const commentsLabel = $derived(!ready ? 'Comments unavailable until the document opens' : commentsLoading ? 'Loading comments'
		: `${commentCount} comment${commentCount === 1 ? '' : 's'}${unreadCount ? `, ${unreadCount} new` : ''}${unsent ? `, ${unsent} waiting for Send` : ''}`);
	const panelId = $derived(`doc-details-${targetInstanceId ?? 'doc'}`);
	const statusText = $derived(freshnessPresentation
		? freshnessPresentation.overdue
			// Overdue names the last verification's date: it fits beside Mark verified at phone width, where an age cuts mid-word.
			? `Overdue · ${freshnessPresentation.dateText ? `verified ${freshnessPresentation.dateText}` : freshnessPresentation.phrase}`
			: freshnessPresentation.phrase
		: '');

	let headerEl = $state<HTMLElement | null>(null);
	let panelEl = $state<HTMLElement | null>(null);
	let panelOpen = $state(false);
	let panelMax = $state<number | null>(null);
	/** The open panel would reach down over the ask's card, so the card steps aside until it closes; Details then carries the ask. */
	let cardCovered = $state(false);

	$effect(() => {
		artifactId;
		panelOpen = false;
		verificationError = '';
		verificationNotice = '';
		verifying = false;
	});

	// The panel takes the height left below its top, so it reaches its last row without scrolling wherever that fits.
	$effect(() => {
		const panel = panelEl;
		if (!panelOpen || !panel) { cardCovered = false; return; }
		const fit = () => {
			const top = panel.getBoundingClientRect().top;
			panelMax = Math.floor(window.innerHeight - top - 8);
			const box = headerEl?.querySelector<HTMLElement>('[data-ask-card]')?.getBoundingClientRect();
			cardCovered = !!box && box.height > 0 && top + Math.min(panel.scrollHeight, panelMax) > box.top - 8;
		};
		fit();
		window.addEventListener('resize', fit);
		return () => window.removeEventListener('resize', fit);
	});

	// A press outside the panel and its button closes it; Escape closes it and returns to the button.
	$effect(() => {
		if (!panelOpen) return;
		const outside = (event: PointerEvent) => {
			const node = event.target as Node | null;
			if (!node || panelEl?.contains(node) || detailsEntry?.contains(node)) return;
			// A picker or review list opened from a row is drawn at the page's top level; pressing in it keeps the panel and that row.
			if ((node instanceof Element ? node : node.parentElement)?.closest('[data-popover], [data-adaptive-drawer]')) return;
			panelOpen = false;
		};
		// Escape closes it only from the panel or its button; anywhere else it belongs to that control (a composer, a sheet).
		const keys = (event: KeyboardEvent) => {
			const node = event.target as Node | null;
			if (event.key !== 'Escape' || !node || !(panelEl?.contains(node) || detailsEntry?.contains(node))) return;
			event.preventDefault();
			event.stopPropagation();
			panelOpen = false;
			detailsEntry?.focus();
		};
		document.addEventListener('pointerdown', outside, true);
		document.addEventListener('keydown', keys, true);
		return () => { document.removeEventListener('pointerdown', outside, true); document.removeEventListener('keydown', keys, true); };
	});

	// The bar that holds the canvas draws this document's controls (barTools, barChange): one instance, wherever they show.
	$effect(() => publishDocumentBar({ tools: barTools, titleEnd: urgent ? barChange : null }));

	function closeThen(fn?: () => void) { panelOpen = false; fn?.(); }

	async function changeImmediateDelivery(value: boolean): Promise<void> {
		if (!(await setDeliverCommentsImmediately(value))) return;
		// Another client may have held a comment this one has not synced yet, so turning delivery on always asks the
		// server to send the open document's held lane. An empty lane delivers nothing and shows nothing.
		if (value) void feedbackStatus?.controller().sendDocument();
	}

	async function markVerified(): Promise<void> {
		if (!onMarkVerified || verifying) return;
		const started = artifactId; // the answer belongs to the document it started on
		verifying = true;
		verificationError = '';
		try {
			await onMarkVerified();
			if (artifactId === started) verificationNotice = 'Document marked verified.';
		} catch (error) {
			if (artifactId === started) verificationError = error instanceof Error ? error.message : 'Could not mark this document verified.';
		} finally {
			if (artifactId === started) verifying = false;
		}
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

<!-- The document's one row: Comments and Details, drawn by the app bar or the tab strip (documentBar). -->
{#snippet barTools()}<span class="bar-tools">
	<button type="button" class="bar-comments" data-comments-entry bind:this={commentsEntry} aria-expanded={commentsOpen} disabled={!ready || commentsLoading}
		aria-label={commentsLabel} onclick={(event) => onOpenComments(event.currentTarget)}>
		<svg aria-hidden="true" viewBox="0 0 16 16"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z" /></svg>
		<!-- A count in text, never a dot: unread reads "25 new". -->
		{#if !ready || commentsLoading}<span>…</span>{:else if unreadCount}<span class="bar-new">{unreadCount} new</span>{:else}<span>{commentCount}</span>{/if}
	</button>
	<!-- Details: a vertical ⋮ (the app bar's More mark turned upright), with an amber count beside it when something behind it needs you. -->
	<button type="button" class="bar-details" data-document-details bind:this={detailsEntry} aria-expanded={panelOpen} aria-controls={panelId}
		aria-label={`Document details${needsCount ? ` · ${needsCount} needs you` : ''}`} title="Document details" onclick={() => panelOpen = !panelOpen}>
		{#if needsCount}<span class="bar-needs" aria-hidden="true">{needsCount}</span>{/if}
		<svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1.4" /><circle cx="12" cy="12" r="1.4" /><circle cx="12" cy="19" r="1.4" /></svg>
	</button>
</span>{/snippet}

<!-- File changed after the document's name in the app bar: the reload mark alone, amber, its name read out and on hover. -->
{#snippet barChange()}
	<button type="button" class="bar-reload stale-control" data-file-changed aria-label="File changed · Reload" title="File changed · Reload" onclick={() => onReload?.()}>
		<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.34-5.66" /><path d="M20 4v5h-5" /></svg>
	</button>
{/snippet}

<header class="document-header" class:card-covered={cardCovered} bind:this={headerEl}>
	{#if reviewBar}{@render reviewBar()}{/if}
	<!-- Always mounted, so Send, the pickers and the review controls keep one instance; hidden while closed. It drops over the document. -->
	<div class="doc-panel" id={panelId} role="region" aria-label="Document details" hidden={!panelOpen} bind:this={panelEl} style:--panel-max={panelMax ? `${panelMax}px` : undefined}>
		<div class="p-section">
			{#if urgent && !changeInBar}<button type="button" class="p-row amber stale-control" data-file-changed onclick={() => closeThen(onReload)}>File changed · Reload</button>{/if}
			{#if ask && (!ask.onCard || cardCovered)}<div class="p-ask" data-details-ask>
				<span class="p-who"><strong><MiddleName name={ask.who} /></strong></span>
				<span class="p-full">{ask.text}</span>
				{#each ask.others as other (other.name)}<span class="p-full"><strong>{other.name}</strong>: {other.reason}</span>{/each}
			</div>{/if}
			{#if freshnessPresentation}<div class="p-row" class:amber={freshnessPresentation.overdue}>
				<span class="p-text"><span class="p-label">Status:</span> <time datetime={freshnessPresentation.datetime}>{statusText}{#if freshnessPresentation.dateText && !statusText.includes(freshnessPresentation.dateText)}<span class="sr-only"> ({freshnessPresentation.dateText})</span>{/if}</time></span>
				{#if freshness && shouldOfferVerification(freshness)}<span class="verification-action"><Button size="sm" variant="ghost" onclick={markVerified} loading={verifying} disabled={headerStandIn}>Mark verified</Button></span>{/if}
			</div>{/if}
			{#if saveStatus !== 'idle'}<div class="p-row p-quiet" role="status">{saveStatus === 'saving' ? 'Saving…' : 'Saved'}</div>{/if}
			{#if readonly && ready}<div class="p-row readonly-note">Read-only</div>{/if}
		</div>
		{#if ownership && artifactId}<div class="p-section p-owner" class:held={headerStandIn} data-ownership-row>{#key artifactId}<DocOwnerControl {artifactId} {ownership} inert={headerStandIn} onChange={onOwnershipChanged ?? (() => {})} />{/key}</div>
		{:else if freshness}<div class="p-section"><div class="p-row">Owner: {freshness.owner_name}</div></div>{/if}
		<div class="p-section">
			<span class="p-label-row">Comments</span>
			{#if artifactId}<span class="p-row p-send"><DocFeedbackStatus bind:this={feedbackStatus} {artifactId} {targetInstanceId} {onFeedbackChanged} {activityRoot} lane="document-held" countSep=" · " /></span>{/if}
			{#if resolvedCount && onOpenResolved}<button type="button" class="p-row resolved-tool" data-resolved-entry aria-haspopup={resolvedInColumn ? undefined : 'dialog'} aria-expanded={resolvedOpen} onclick={() => closeThen(onOpenResolved)}>Resolved · {resolvedCount}</button>{/if}
			{#if artifactId}<span class="p-row delivery-toggle" title="When off, comments send when you close the document or pause for two minutes."><span class="p-text">Deliver immediately</span><Toggle size="sm" touch checked={$deliverCommentsImmediately} onchange={changeImmediateDelivery} ariaLabel="Deliver comments immediately" /></span>{/if}
		</div>
		{#if artifactId || (historyAvailable && onOpenHistory)}<div class="p-section">
			<span class="p-label-row">Review</span>
			{#if artifactId}<span class="p-row p-review"><DocReviewControls {artifactId} {viewingThreadId} targetKey={reviewTargetKey ?? targetInstanceId} restoreTo={() => detailsEntry} /></span>{/if}
			{#if historyAvailable && onOpenHistory}<button type="button" class="p-row history-tool" aria-expanded={historyOpen} onclick={() => closeThen(onOpenHistory)}>History</button>{/if}
		</div>{/if}
		{#if paneHere}<div class="p-section"><button type="button" class="p-row" onclick={() => closeThen(() => paneOpen.set(true))}>Show panel</button></div>{/if}
		{#if presence}<div class="p-section p-presence"><span class="p-label-row">Recent activity</span>{@render presence()}</div>{/if}
	</div>
	<DocLiveStatus session={liveSession} {openRefusal} {liveUnavailable} {tabText} {onReopen} {onRead} connectionStatusId={connectionDescriptionId} />
	<span class="metadata-status" class:sr-only={!verificationError} class:metadata-error={!!verificationError} role="status">{verificationError || verificationNotice || staleAnnouncement}</span>
</header>

<style>
	/* No row of its own: the document's controls are in the app bar or the tab strip; this holds the ask's card, the Details panel and the live status. */
	.document-header { grid-area: header; min-width: 0; flex: none; display: flex; flex-direction: column; padding-left: var(--doc-inset, 24px); background: var(--bg-primary); }
	.metadata-error { color: var(--red); }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }

	/* The bar's controls: 36px with a pointer, 44px at phone width (IconButton lg's sizes), 16px apart (the hosts' gap). */
	.bar-comments, .bar-details, .bar-reload { --bar-row: 36px; flex: none; display: inline-flex; align-items: center; justify-content: center; gap: var(--space-1); height: var(--bar-row); min-width: var(--bar-row); padding: 0 var(--space-1); border: 0; border-radius: var(--radius-sm); background: none; color: var(--text-secondary); font: inherit; font-size: var(--text-footnote); font-weight: var(--fw-regular); white-space: nowrap; cursor: pointer; }
	@media (max-width: 768px) { .bar-comments, .bar-details, .bar-reload { --bar-row: var(--touch-target); } }
	.bar-comments:hover:not(:disabled), .bar-details:hover { background: var(--bg-hover); color: var(--text-primary); }
	/* Comments and Details, 16px apart, in whichever bar draws them. */
	.bar-tools { display: inline-flex; flex: none; align-items: center; gap: var(--space-4); }
	.bar-comments:disabled { opacity: 0.5; cursor: not-allowed; }
	.bar-comments:focus-visible, .bar-details:focus-visible, .bar-reload:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	.bar-comments svg { width: var(--text-subhead); height: var(--text-subhead); fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linejoin: round; }
	.bar-new { color: var(--accent-text); font-weight: var(--fw-medium); }
	.bar-details { padding: 0 var(--space-2); }
	.bar-details svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2.4; stroke-linecap: round; }
	.bar-details[aria-expanded='true'] { background: var(--bg-hover); color: var(--text-primary); }
	.bar-needs { color: var(--amber-text); font-size: var(--text-footnote); font-weight: var(--fw-semibold); }
	.bar-reload { width: var(--touch-target); height: var(--touch-target); padding: 0; color: var(--amber-text); }
	.bar-reload svg { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }

	/* The panel drops over the document (it never moves the text), on the Popover primitive's solid surface: the document would read through a translucent one. */
	.doc-panel { position: absolute; z-index: 6; top: var(--space-1); right: var(--space-2); width: min(360px, calc(100% - 2 * var(--space-2))); max-height: var(--panel-max, min(70vh, 620px)); overflow: auto; box-sizing: border-box; display: flex; flex-direction: column; padding: var(--space-3); border: 1px solid var(--border-subtle); border-radius: var(--radius); background: var(--bg-elevated); box-shadow: var(--glass-elevation-2); font-size: var(--text-footnote); color: var(--text-secondary); }
	.doc-panel[hidden] { display: none; }
	.p-section { display: flex; flex-direction: column; min-width: 0; }
	.p-section:empty { display: none; }
	/* One rhythm: rows 32px apart with a pointer, 44px at phone width, no gap between sections; a section label opens its group with 8px above it. */
	.p-label-row { padding: var(--space-2) 0 var(--space-1); color: var(--text-tertiary); font-size: var(--text-caption); font-weight: var(--fw-medium); }
	.p-row { display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); min-height: var(--space-7); min-width: 0; padding: 0; border: 0; border-radius: var(--radius-sm); background: none; color: var(--text-primary); font: inherit; text-align: left; cursor: pointer; }
	div.p-row, span.p-row { cursor: default; }
	.p-row.amber { color: var(--amber-text); }
	.p-row.p-quiet { color: var(--text-tertiary); }
	.p-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	/* A row's action keeps its whole label; the row's text is what shortens (Mark verified beside a long Status line on a phone). */
	.verification-action { flex: none; display: inline-flex; min-height: inherit; }
	.p-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.p-label { color: var(--text-secondary); font-weight: var(--fw-regular); }
	/* An empty Send row (nothing held, sending or failed) is not drawn. */
	.p-send:not(:has(*)) { display: none; }
	/* Send takes its short 'Send · N' wording at every window width, as Resolved · N does beside it. */
	.p-send { container-type: inline-size; }
	/* Owner and supervisor are rows too: the same 32px with a pointer. Only there: with touch, DocOwnerControl's own 44px must win. */
	@media (min-width: 769px) and (not (any-pointer: coarse)) { .p-owner :global(.ownership > .item) { min-height: var(--space-7); } }
	/* A remembered row waiting for its load is drawn exactly as the loaded one; its controls only don't act yet. */
	.p-owner.held :global(button:disabled) { opacity: 1; cursor: default; }
	/* Send and Request review are text actions in the panel, starting at its text edge: no outline, inset or centring. */
	.doc-panel :global(button:not([role="switch"])) { justify-content: flex-start; }
	.doc-panel.doc-panel :global(.btn.btn) { padding-inline: 0; border-width: 0; }
	.p-send :global(button), .p-review :global(.review-request) { min-height: inherit; padding: 0; border: 0; border-radius: var(--radius-sm); background: none; box-shadow: none; color: var(--text-primary); font: inherit; }
	.p-review :global(.review-controls) { display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); width: 100%; }
	.p-review :global(.review-request) { order: -1; }
	.p-review :global(.review-request svg) { display: none; }
	.p-review :global(.review-request .label) { display: inline; }
	/* The ask in full, while its card is not on screen: the name on one line, cut in the middle; the question wraps as prose. */
	.p-ask { display: flex; flex-direction: column; gap: var(--space-1); padding-block: var(--space-1); line-height: var(--lh-normal); }
	.p-who { display: flex; min-width: 0; white-space: nowrap; }
	.p-who strong { display: grid; min-width: 0; }
	.p-ask strong { color: var(--text-primary); font-weight: var(--fw-semibold); }
	.p-full { overflow-wrap: break-word; }
	/* Recent activity only when there is any. */
	.p-presence:not(:has(:global(.presence-row))) { display: none; }
	@media (max-width: 768px) {
		.p-row { min-height: var(--touch-target); --toggle-hit: var(--touch-target); }
		.doc-panel :global(button:not([role="switch"])), .p-review :global(.review-request) { min-height: var(--touch-target); min-width: var(--touch-target); }
	}
</style>
