<script lang="ts">
	import DocFeedbackStatus from './DocFeedbackStatus.svelte';
	import DocLiveStatus from './DocLiveStatus.svelte';
	import DocOwnerControl from './DocOwnerControl.svelte';
	import MiddleName from './MiddleName.svelte';
	import type { DocViewerStatusProps } from './docViewerStatusProps';
	import DocReviewControls from './DocReviewControls.svelte';
	import { Button } from '$lib/components/ui';
	import Spinner from './ui/Spinner.svelte';
	import { formatFreshness, shouldOfferVerification } from '$lib/docFreshness';
	import { detailsNeedsUp } from '$lib/docDetailsNeeds';
	import type { CommentFeedback } from '$lib/api/docs';
	import type { DocFeedback } from '$lib/docFeedback.svelte';
	import { liveConnectionDescriptionId } from '$lib/liveConnectionDescription';
	import { publishDocumentBar } from '$lib/stores/documentBar';
	import { canvasCompact } from '$lib/stores/artifactCanvas';
	import { isMobile } from '$lib/stores/viewport';
	import { paneOpen } from '$lib/stores/paneOpen';
	import { docSaveDrafts } from '$lib/stores/docSaveQueue';

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
		offScreen = false,
		activityRoot = null,
		historyAvailable = false,
		historyOpen = false,
		onOpenHistory,
		commentsEntry = $bindable(null),
		detailsEntry = $bindable(null),
	}: DocViewerStatusProps = $props();

	let feedbackStatus = $state<DocFeedbackStatus>();
	let verifying = $state(false);
	let verificationError = $state('');
	let verificationNotice = $state('');
	// While a typing save is refused for a changed file, the Details beside the refusal is the way on, so Reload is not offered beside it.
	const staleControl = $derived(stale && ready && !$docSaveDrafts[targetInstanceId ?? '']?.stale);
	// A live region only announces a change, so the stale state needs text that appears when it starts.
	const staleAnnouncement = $derived(staleControl ? 'File changed since you opened it. Reload to see the latest.' : '');
	const freshnessPresentation = $derived(freshness ? formatFreshness(freshness) : null);
	// The live-status row renders the stable connection description for unavailable actions.
	const connectionDescriptionId = $derived(liveConnectionDescriptionId(liveSession?.artifactId));
	/** File changed, until Reload clears it (and not offered beside a refused typing save). */
	const urgent = $derived(staleControl);
	/** In the compact canvas, File changed sits after the name; the desktop tab strip draws the text control. */
	const changeInBar = $derived($canvasCompact);
	/** On a phone with the panel closed, the app bar's Toggle panel is here instead, as Show panel. */
	const paneHere = $derived($isMobile && $canvasCompact && !$paneOpen);
	/** What needs you behind Details and is nowhere else on screen, named in the words the panel itself uses for each
	 * entry, so the control says what is waiting rather than how many: Overdue, and an ask whose card is hidden, an ask
	 * with no reason text named by its asker rather than leaving an empty leg. The amber count is this list's length, so
	 * the number and the name can't drift apart. */
	const needsUp = $derived(detailsNeedsUp(
		{ overdue: !!freshnessPresentation?.overdue, hiddenAsk: !!ask && !ask.onCard },
		{ overdue: 'Overdue', hiddenAsk: ask ? ask.text.trim() || `${ask.who} asks` : '' }));
	const needsCount = $derived(needsUp.length);
	// The unsent comments, for the Comments button's accessible summary (CommentsPill's naming); the batch itself is the one source.
	const unsent = $derived(feedbackStatus?.controller().pendingCount ?? 0);
	const sent = $derived((feedbackStatus?.controller().sent ?? 0) > 0);
	let receiptBox = $state<HTMLElement>(), receiptMeasure = $state<HTMLElement>();
	let receiptTextFits = $state(false);
	$effect(() => {
		const box = receiptBox, measure = receiptMeasure;
		if (!box || !measure) return;
		const update = () => { receiptTextFits = measure.getBoundingClientRect().width <= box.getBoundingClientRect().width; };
		update();
		const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
		observer?.observe(box); observer?.observe(measure);
		return () => observer?.disconnect();
	});
	const commentsLabel = $derived(!ready ? 'Comments unavailable until the document opens' : commentsLoading ? 'Loading comments'
		: `${commentCount} comment${commentCount === 1 ? '' : 's'}${unreadCount ? `, ${unreadCount} unread` : ''}${unsent ? `, ${unsent} waiting for Send` : ''}${sent && !commentsOpen ? ', Sent' : ''}`);
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
	/** The open panel would reach down over the ask (its card on a phone, its row in the header), so the ask steps aside until it closes; Details then carries it. */
	let askCovered = $state(false);

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
		if (!panelOpen || !panel) { askCovered = false; return; }
		const fit = () => {
			// The document can sit beside the bar entry; keep its panel within the window in either position.
			panel.style.removeProperty('--panel-shift');
			const rect = panel.getBoundingClientRect();
			const shift = rect.left < 8 ? 8 - rect.left : Math.min(0, window.innerWidth - 8 - rect.right);
			panel.style.setProperty('--panel-shift', `${shift}px`);
			const top = rect.top;
			panelMax = Math.floor(window.innerHeight - top - 8);
			const box = headerEl?.querySelector<HTMLElement>('[data-ask-card], .review-bar.ask')?.getBoundingClientRect();
			askCovered = !!box && box.height > 0 && top + Math.min(panel.scrollHeight, panelMax) > box.top - 8;
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

	// The bar that holds the canvas draws this document's controls (barTools, barChange): one instance, wherever they show. Off
	// screen the document publishes nothing, so the bar never offers a list or panel for a document the person can't see.
	$effect(() => offScreen ? undefined : publishDocumentBar({ tools: barTools, titleEnd: urgent ? barChange : null }));

	function closeThen(fn?: () => void) { panelOpen = false; fn?.(); }

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
	/** The bar's Comments button, which the comments drawer opens from and returns focus to. */
	export function commentsButton(): HTMLButtonElement | null { return commentsEntry; }
</script>

<!-- The document's one row: Comments and Details, drawn by the app bar or the tab strip (documentBar). -->
{#snippet barTools()}<span class="bar-tools">
	{#if presence}{@render presence()}{/if}
	{#if urgent && !changeInBar}<button type="button" class="bar-reload bar-stale stale-control" data-file-changed aria-label="File changed · Reload" title="File changed · Reload" onclick={() => onReload?.()}>
		<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.34-5.66" /><path d="M20 4v5h-5" /></svg>
		<!-- The strip's own room decides whether the words show; the ↻ mark is the phone header's compact form. -->
		<span class="bar-stale-word">File changed · Reload</span>
	</button>{/if}
	<!-- The transient save cue yields to File changed's reload mark so the name keeps its room. -->
	{#if saveStatus !== 'idle'}<span class="bar-save" class:saving={saveStatus === 'saving'} class:yields={urgent && changeInBar}>{saveStatus === 'saving' ? 'Saving…' : '✓ Saved'}</span>{/if}
	<button type="button" class="bar-comments" data-comments-entry bind:this={commentsEntry} aria-expanded={commentsOpen} disabled={!ready || commentsLoading}
		aria-busy={!ready || commentsLoading} aria-label={commentsLabel} onclick={(event) => onOpenComments(event.currentTarget)}>
		<span class="bar-comments-content" class:receipt-hidden={sent && !commentsOpen}>
			<svg aria-hidden="true" viewBox="0 0 16 16"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z" /></svg>
			<!-- Unread is text; the separate dot marks comments waiting for Send. -->
			{#if !ready || commentsLoading}<Spinner size={14} />{:else if unreadCount}<span class="bar-new">{unreadCount}<span class="bar-new-word">{' unread'}</span></span>{:else}<span>{commentCount}</span>{/if}
			{#if unsent}<span class="unsent-dot" aria-hidden="true"></span>{/if}
		</span>
		{#if sent && !commentsOpen}<span class="bar-sent-container" data-comments-sent aria-hidden="true" bind:this={receiptBox}>
			<span class="bar-sent"><svg viewBox="0 0 16 16"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z" /></svg><svg class="sent-check" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5" /></svg>{#if receiptTextFits}<span>Sent</span>{/if}</span>
			<span class="bar-sent-measure" bind:this={receiptMeasure}><svg viewBox="0 0 16 16"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z" /></svg><svg class="sent-check" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5" /></svg><span>Sent</span></span>
		</span>{/if}
	</button>
	<!-- Details: a vertical ⋮ (the app bar's More mark turned upright), with an amber count beside it when something behind it needs you. -->
	<button type="button" class="bar-details" data-document-details bind:this={detailsEntry} aria-expanded={panelOpen} aria-controls={panelId}
		aria-label={`Document details${needsUp.length ? ` · ${needsUp.join(' · ')}` : ''}`} title="Document details" onclick={() => panelOpen = !panelOpen}>
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

{#if artifactId}<DocFeedbackStatus bind:this={feedbackStatus} {artifactId} {targetInstanceId} {onFeedbackChanged} {activityRoot} lane="document-held" showControls={false} />{/if}
<header class="document-header" class:ask-covered={askCovered} bind:this={headerEl}>
	{#if reviewBar}{@render reviewBar()}{/if}
	<!-- The pickers and review controls keep one instance while Details is closed. It drops over the document. -->
	<div class="doc-panel" id={panelId} role="region" aria-label="Document details" hidden={!panelOpen} bind:this={panelEl} style:--panel-max={panelMax ? `${panelMax}px` : undefined}>
		<div class="p-section">
			{#if ask && (!ask.onCard || askCovered)}<div class="p-ask" data-details-ask>
				<span class="p-who"><strong><MiddleName name={ask.who} /></strong></span>
				<span class="p-full">{ask.text}</span>
				{#each ask.others as other (other.name)}<span class="p-full"><strong>{other.name}</strong>: {other.reason}</span>{/each}
			</div>{/if}
			{#if freshnessPresentation}<div class="p-row" class:amber={freshnessPresentation.overdue}>
				<span class="p-text"><span class="p-label">Status:</span> <time datetime={freshnessPresentation.datetime}>{statusText}{#if freshnessPresentation.dateText && !statusText.includes(freshnessPresentation.dateText)}<span class="sr-only"> ({freshnessPresentation.dateText})</span>{/if}</time></span>
				{#if freshness && shouldOfferVerification(freshness)}<span class="verification-action"><Button size="sm" variant="ghost" onclick={markVerified} loading={verifying} disabled={headerStandIn}>Mark verified</Button></span>{/if}
			</div>{/if}
			<!-- The bar says Saving… while it saves; Details keeps the settled state. -->
			{#if saveStatus === 'saved'}<div class="p-row p-quiet" role="status">Saved</div>{/if}
			{#if readonly && ready}<div class="p-row readonly-note">Read-only</div>{/if}
		</div>
		{#if ownership && artifactId}<div class="p-section p-owner" class:held={headerStandIn} data-ownership-row>{#key artifactId}<DocOwnerControl {artifactId} {ownership} inert={headerStandIn} onChange={onOwnershipChanged ?? (() => {})} />{/key}</div>
		{:else if freshness}<div class="p-section"><div class="p-row">Owner: {freshness.owner_name}</div></div>{/if}
		{#if artifactId || (historyAvailable && onOpenHistory)}<div class="p-section">
			<span class="p-label-row">Review</span>
			{#if artifactId}<span class="p-row p-review"><DocReviewControls {artifactId} {viewingThreadId} targetKey={reviewTargetKey ?? targetInstanceId} restoreTo={() => detailsEntry} /></span>{/if}
			{#if historyAvailable && onOpenHistory}<button type="button" class="p-row history-tool" aria-expanded={historyOpen} onclick={() => closeThen(onOpenHistory)}>History</button>{/if}
		</div>{/if}
		{#if paneHere}<div class="p-section"><button type="button" class="p-row" onclick={() => closeThen(() => paneOpen.set(true))}>Show panel</button></div>{/if}
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
	/* File changed is one control with two forms: the words, which size the button (dev's 135x32 text button), and the ↻ mark,
	 * which is the square below. Its width is never fixed, or the label paints past the box and over its neighbour. */
	.bar-reload.bar-stale { height: 32px; padding: 0 var(--space-2); background: var(--amber-bg); font-weight: var(--fw-medium); }
	/* Comments and Details, 16px apart, in whichever bar draws them. */
	.bar-tools { display: inline-flex; flex: none; align-items: center; gap: var(--space-4); }
	@media (max-width: 768px) {
		:global(.shell-header) .bar-tools { gap: var(--space-1); }
		:global(.shell-header) .bar-new-word { display: none; }
	}
	.bar-tools :global(.presence-row) { flex: none; }
	.bar-comments { position: relative; }
	.bar-comments-content { display: inline-flex; align-items: center; gap: var(--space-1); }
	.bar-comments-content.receipt-hidden { visibility: hidden; }
	.bar-sent-container { position: absolute; inset: 0; overflow: hidden; }
	.bar-sent, .bar-sent-measure { display: flex; align-items: center; color: var(--green-text); }
	.bar-sent { position: absolute; inset: 0; justify-content: center; }
	.bar-sent-measure { position: absolute; width: max-content; visibility: hidden; pointer-events: none; }
	.bar-comments .bar-sent svg, .bar-comments .bar-sent-measure svg { flex: none; }
	.bar-comments .sent-check { width: 12px; height: 12px; stroke-width: 2.4; }
	.bar-comments:disabled { opacity: 0.5; cursor: not-allowed; }
	.bar-comments:focus-visible, .bar-details:focus-visible, .bar-reload:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	/* The tab strip's own room decides, not the window's: the row gives way in the order the phone header gives way in,
	 * and the document tab is never what is shortened first. The svg rides in the control so the narrow form is the
	 * approved phone header's ↻ mark, with the same accessible name and tooltip. */
	.bar-reload.bar-stale svg { display: none; }
	/* rem, not px: the room this bar needs is set by its text, so the rungs move with the reader's font size.
	 * 35.5rem = 568px, 34rem = 544px at the default 16px root. */
	@container strip (max-width: 35.5rem) {
		/* 1. The tight rung: the row's own gaps and side padding, as the phone row wears them. */
		.bar-tools { gap: var(--space-1); }
	}
	@container strip (max-width: 34rem) {
		/* 2. File changed takes the ↻ mark, so its words stop costing the tab its room. */
		.bar-reload.bar-stale { width: var(--bar-row); height: var(--bar-row); padding: 0; background: none; font-weight: var(--fw-regular); }
		.bar-reload.bar-stale svg { display: block; width: 1.25rem; height: 1.25rem; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
		.bar-stale-word { display: none; }
	}
	/* Both forms tint on hover, as dev's File changed did; written after the narrow rung so its fill reset cannot take it away. */
	.bar-reload.bar-stale:hover { background: color-mix(in srgb, var(--amber) 18%, transparent); }
	.bar-comments svg { width: var(--text-subhead); height: var(--text-subhead); fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linejoin: round; }
	.bar-new { color: var(--accent-text); font-weight: var(--fw-medium); }
	.unsent-dot { width: var(--space-2); height: var(--space-2); border-radius: var(--radius-circle); background: var(--accent); }
	.bar-details { padding: 0 var(--space-2); }
	.bar-details svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2.4; stroke-linecap: round; }
	.bar-details[aria-expanded='true'] { background: var(--bg-hover); color: var(--text-primary); }
	.bar-tools .bar-save { flex: none; display: inline-flex; align-items: center; box-sizing: border-box; border-radius: var(--radius-pill); color: var(--text-tertiary); font-size: var(--text-footnote); font-weight: var(--fw-regular); white-space: nowrap; animation: fadein var(--dur-tight) ease-out; }
	.bar-tools .bar-save:not(.saving) { color: var(--green); }
	.bar-tools .bar-save.yields { display: none; }
	.bar-needs { color: var(--amber-text); font-size: var(--text-footnote); font-weight: var(--fw-semibold); }
	.bar-reload { color: var(--amber-text); }
	/* The app bar's mark after the document's name is the square; the strip's words form keeps its label's width. */
	.bar-reload:not(.bar-stale) { width: var(--touch-target); height: var(--touch-target); padding: 0; }
	.bar-reload svg { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }

	/* The panel drops over the document (it never moves the text), on the Popover primitive's solid surface: the document would read through a translucent one. */
	.doc-panel { position: absolute; z-index: 6; top: var(--space-1); right: var(--space-2); width: min(360px, calc(100% - 2 * var(--space-2)), calc(100vw - 2 * var(--space-2))); transform: translateX(var(--panel-shift, 0px)); max-height: var(--panel-max, min(70vh, 620px)); overflow: auto; box-sizing: border-box; display: flex; flex-direction: column; padding: var(--space-3); border: 1px solid var(--border-subtle); border-radius: var(--radius); background: var(--bg-elevated); box-shadow: var(--glass-elevation-2); font-size: var(--text-footnote); color: var(--text-secondary); }
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
	/* Owner and supervisor are rows too: the same 32px with a pointer. Only there: with touch, DocOwnerControl's own 44px must win. */
	@media (min-width: 769px) and (not (any-pointer: coarse)) { .p-owner :global(.ownership > .item) { min-height: var(--space-7); } }
	/* A remembered row waiting for its load is drawn exactly as the loaded one; its controls only don't act yet. */
	.p-owner.held :global(button:disabled) { opacity: 1; cursor: default; }
	/* Request review is a text action at the panel's text edge. */
	.doc-panel :global(button:not(.tg)) { justify-content: flex-start; }
	.doc-panel.doc-panel :global(.btn.btn) { padding-inline: 0; border-width: 0; }
	.p-review :global(.review-request) { min-height: inherit; padding: 0; border: 0; border-radius: var(--radius-sm); background: none; box-shadow: none; color: var(--text-primary); font: inherit; }
	/* The review controls pass the row's height down, so Request review and the reviewers are row-tall targets, not text-tall. */
	.p-review :global(.review-controls) { display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); width: 100%; min-height: inherit; }
	.p-review :global(.reviewer-group) { min-height: inherit; }
	.p-review :global(.review-request) { order: -1; }
	.p-review :global(.review-request svg) { display: none; }
	.p-review :global(.review-request .label) { display: inline; }
	/* The ask in full, while its card is not on screen: the name on one line, cut in the middle; the question wraps as prose. */
	.p-ask { display: flex; flex-direction: column; gap: var(--space-1); padding-block: var(--space-1); line-height: var(--lh-normal); }
	.p-who { display: flex; min-width: 0; white-space: nowrap; }
	.p-who strong { display: grid; min-width: 0; }
	.p-ask strong { color: var(--text-primary); font-weight: var(--fw-semibold); }
	.p-full { overflow-wrap: break-word; }
	@media (max-width: 768px) {
		.p-row { min-height: var(--touch-target); --toggle-hit: var(--touch-target); }
		.doc-panel :global(button:not(.tg)), .p-review :global(.review-request) { min-height: var(--touch-target); min-width: var(--touch-target); }
	}
</style>
