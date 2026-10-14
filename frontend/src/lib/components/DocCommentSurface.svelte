<script lang="ts">
	import { tick } from 'svelte';
	import { keptThreads } from '$lib/docKeptThreads.svelte';
	import { get } from 'svelte/store';
	import { Button, IconButton } from './ui';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { menuPop, peerFade } from '$lib/popoverMotion';
	import type { CommentLoad } from '$lib/stores/docComments';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import Popover from './ui/Popover.svelte';
	import CommentComposer from './comments/CommentComposer.svelte';
	import CommentList from './comments/CommentList.svelte';
	import CommentPlacementRecovery, { type PlacementRecoveryProps } from './comments/CommentPlacementRecovery.svelte';
	import ImageRegionThumb from './ImageRegionThumb.svelte';
	import { decideSuggestion, SuggestionRefused, type SuggestionDecision } from '$lib/api/commentSuggestions';
	import { flushDraft, seedDraft } from '$lib/stores/docSaveQueue';
	import { loadComments, type DocComment } from '$lib/stores/docComments';
	import { clickedOpener, drawerFooterHeight, drawerKeys, footerFocusHome, openerFocus, presence } from '$lib/docCommentDrawer';
	import { createCommentSheet } from '$lib/docCommentSheet.svelte';
	import { imageAlt, imageGroupKey } from '$lib/docImageComments';
	import { suggestionRefusals, recordRefusal, clearRefusal, clearRefusals } from '$lib/stores/suggestionRefusals';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { CommentAnchor } from '$lib/docCommentSurfaceState.svelte';
	import type { DocFeedback } from '$lib/docFeedback.svelte';
	type SurfaceState = { kind: 'draft' } | { kind: 'thread'; id: string } | { kind: 'list' } | null;
	/**
	 * Comments on an image (DCOL-FR13.6): its thumbnail heads the list, cards carry their tag's number and colour.
	 * `hoveredId` and `onHover` link each card with its mark on the image; the drawer scrolls to the selected card itself.
	 */
	export type ImageCommentSurface = { src: string; alt?: string; colorOf: (comment: DocComment) => string; onMovePin?: (id: string) => void; onOpen?: () => void; showThumbnail?: boolean; imageBounds?: () => DOMRect | null; hoveredId?: string | null; onHover?: (id: string | null) => void; picks?: number };
	/**
	 * A document's images with comments on parts of them (DCOL-FR13.6): each image's comments sit together in the drawer
	 * under its thumbnail, and their cards carry their tag's number and colour. `src` is null for an image with no file.
	 */
	export type DocImageComments = { srcOf: (comment: DocComment) => string | null; colorOf: (comment: DocComment) => string; onOpen: (comment: DocComment, focus?: { id: string; move?: boolean }) => void };
	let { state: surfaceState, anchor, bounds = null, narrow, comments, focusedId, draft, draftReason = '', onDraftBody, onCancelDraft, onClose, restoreFocus, onFocus, onSubmit, onReply, onResolve, onAgree, onViewed, target, artifactId, readOnly = false, onSuggestionBusy, onSuggestionAccepted, onDocumentChanged, load, onRetry, image = null, images = null, feedback = null, placementRecovery = { orphanedCount: 0 }, covered = false, onDrawerShown }: {
		state: SurfaceState;
		anchor: CommentAnchor | null;
		/** The canvas the card stays inside. */
		bounds?: HTMLElement | null;
		narrow: boolean;
		comments: DocComment[];
		focusedId: string | null;
		/** `label` names what a non-text comment is on, in place of the quote. */
		draft: { text: string; body?: string; label?: string } | null;
		draftReason?: string;
		onDraftBody?: (text: string) => void;
		onCancelDraft?: () => void;
		onClose: () => void;
		restoreFocus: () => void;
		onFocus: (id: string, placement?: 'top') => void;
		onSubmit: (text: string) => Promise<void>;
		onReply: (id: string, text: string) => Promise<void>;
		onResolve: (id: string, resolved: boolean) => void;
		onAgree: (id: string, agreed: boolean) => void;
		onViewed?: (id: string, replyId: string) => Promise<void>;
		target: DocViewerTarget | null;
		artifactId: string | null;
		readOnly?: boolean;
		/** A decision is in flight (`busy`): the editor holds still, and while it is an Accept the suggestion `id` is marked busy. */
		onSuggestionBusy?: (busy: boolean, id?: string) => void;
		onSuggestionAccepted?: (text: string) => void;
		/** The server applied an accepted edit but could not record the decision: read the shown document again. */
		onDocumentChanged?: () => void;
		load?: CommentLoad;
		onRetry?: () => void;
		image?: ImageCommentSurface | null;
		images?: DocImageComments | null;
		/** The open document's batch: while it holds messages not sent yet the drawer ends in their Send now footer. */
		feedback?: DocFeedback | null;
		/** Re-resolves orphaned live comment ranges from the existing comments drawer. */
		placementRecovery?: PlacementRecoveryProps;
		/** An image opened from the document covers it, the drawer with it: the drawer stays open beneath, and gets its focus back after. */
		covered?: boolean;
		/** Whether the comment list's drawer is on screen, in either form: true from its mount until its outro has ended. */
		onDrawerShown?: (shown: boolean) => void;
	} = $props();
	const asDoc = (comment: unknown) => comment as DocComment;
	const listDrawerShown = presence((shown) => onDrawerShown?.(shown));
	/** A comment still on its image in the document; one whose image markdown is gone has nowhere to open. */
	const onItsImage = (comment: DocComment) => !comment.orphaned && comment.from != null;
	function movePinInDocument(id: string) {
		const comment = comments.find((item) => item.id === id);
		if (comment && onItsImage(comment)) images?.onOpen(comment, { id, move: true });
	}
	let filter = $state<'all' | 'open' | 'resolved'>('open');
	let filterOpen = $state(false);
	const FILTERS = [['open', 'Open'], ['resolved', 'Resolved'], ['all', 'All']] as const;
	const filterName = $derived(FILTERS.find(([key]) => key === filter)?.[1] ?? 'Open');
	let drawer = $state<HTMLElement>();
	let canvasWidth = $state(Number.POSITIVE_INFINITY);
	$effect(() => {
		if (!bounds || typeof ResizeObserver === 'undefined') return;
		const read = () => canvasWidth = bounds.getBoundingClientRect().width;
		read(); const observer = new ResizeObserver(read); observer.observe(bounds);
		return () => observer.disconnect();
	});
	// Image viewers already decide whether their canvas is a sheet; document canvases use the 900 px rule.
	const sheetDrawer = $derived(narrow || (!image && canvasWidth < 900));
	let detent = $state<'full' | 'half'>('full');
	let replyDrafts = $state<Record<string, string>>({});
	let replySelections = $state<Record<string, [number, number]>>({});
	// A thread someone else resolves while a reply to it is being typed stays in the Open list, as a resolved card, until that reply is sent or cleared.
	const held = keptThreads({ comments: () => comments, drafts: () => replyDrafts, leavesOnRelease: () => filter === 'open', reduced: () => get(prefersReducedMotion), crossfaded: () => get(prefersReducedMotion) && sheetDrawer && surfaceState?.kind === 'list' });
	const column = $derived(bounds?.closest<HTMLElement>('.leaf-body') ?? bounds);
	const openCount = $derived(comments.filter((comment) => !comment.resolved).length);
	// Open counts threads that are open: the resolved card kept for an unsent reply says why it is still listed, and the document's chip agrees.
	const headerCount = $derived(filter === 'open' ? `${openCount} open` : filter === 'resolved' ? `${comments.length - openCount} resolved` : `${comments.length} ${comments.length === 1 ? 'thread' : 'threads'}`);
	const sheet = createCommentSheet({
		state: () => surfaceState,
		draft: () => draft,
		sheetDrawer: () => sheetDrawer,
		narrow: () => narrow,
		onClose: () => onClose(),
		onCancelDraft: () => (onCancelDraft ?? onClose)(),
	});
	// A boolean, so effects that branch on it do not re-run each time the image props object is rebuilt (every hover).
	const isImage = $derived(!!image);
	const shouldShowReply = $derived(!isImage && narrow && focusedId !== null);
	const picks = $derived(image?.picks ?? 0);
	let navigationNotice = $state('');
	const byNumber = (a: DocComment, b: DocComment) => (a.regionNumber ?? Infinity) - (b.regionNumber ?? Infinity);
	// What is shown follows `sheet.state`: the surface's own state, or the one a closing sheet still shows through its outro.
	const displayed = $derived.by(() => {
		const showing = sheet.state;
		if (showing?.kind === 'thread') return comments.filter((comment) => comment.id === showing.id);
		const shown = comments.filter((comment) => filter === 'all' || (filter === 'resolved' ? comment.resolved : !comment.resolved || held.kept(comment) || held.leaving.has(comment.id)));
		// An image's comments read in number order, like its tags.
		return image ? [...shown].sort(byNumber) : shown;
	});
	const followed = $derived(surfaceState?.kind === 'thread' ? comments.find((item) => item.id === surfaceState.id) : undefined);
	// Leaving the document sends the batch (DocFeedback.leave); in its grace period it says when it goes instead.
	const unsentText = $derived.by(() => {
		if (!feedback?.pendingCount) return null;
		const count = `${feedback.pendingCount} message${feedback.pendingCount === 1 ? '' : 's'} not sent yet`;
		const owner = feedback.ownerName ?? 'the owner';
		if (feedback.sendingGrace) return { before: `${count} · sending to `, owner, after: ` in ${feedback.graceMinutes} min` };
		return { before: `${count} · ${feedback.pendingCount === 1 ? 'it goes' : 'they go'} to `, owner, after: ' when you leave.' };
	});
	// A card that follows its text closes once its comment is deleted: there is nothing left to show.
	$effect(() => {
		if (surfaceState?.kind !== 'thread' || typeof anchor !== 'function' || load?.state === 'loading') return;
		if (!followed) onClose();
	});
	// Where the followed card last sat. A comment that is resolved or has lost its text has no place in the text any more, but its
	// card stays open there: closing it would take the refusal reason and any reply being typed with it.
	let lastRect: { id: string; rect: DOMRect } | null = null;
	const cardAnchor = $derived.by((): CommentAnchor | null => {
		const follow = anchor, id = surfaceState?.kind === 'thread' ? surfaceState.id : null;
		if (typeof follow !== 'function' || id === null) return anchor;
		const placeless = !!followed && (followed.resolved || !!followed.orphaned);
		return () => {
			const rect = placeless && lastRect?.id === id ? lastRect.rect : follow();
			if (rect) lastRect = { id, rect };
			return rect;
		};
	});
	$effect(() => {
		if (surfaceState?.kind !== 'list') return;
		filter = isImage ? 'all' : 'open'; filterOpen = false; navigationNotice = ''; detent = 'full';
		void tick().then(() => {
			const list = drawer?.querySelector<HTMLElement>('.drawer-comments');
			// An image's drawer opens at the selected comment instead (the effect below).
			if (list && !(isImage && focusedId)) list.scrollTop = 0;
			drawer?.querySelector<HTMLElement>('[data-drawer-filter]')?.focus();
		});
	});
	/** Bring a card into view in the drawer's own list: smooth, or at once when asked or with reduced motion. */
	function scrollToCard(id: string, instant: boolean) {
		const list = drawer?.querySelector<HTMLElement>('.drawer-comments');
		const card = list?.querySelector<HTMLElement>(`[data-comment-id="${id}"]`);
		if (!list || !card) return;
		const top = card.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop - 4;
		list.scrollTo({ top: Math.max(0, top), behavior: instant || get(prefersReducedMotion) ? 'instant' : 'smooth' });
	}
	// An image's selected comment, picked on the image or there when the drawer opens, is scrolled to and marked in it.
	// The drawer opens already at its card, so only the drawer moves; a pick while it is open glides the list.
	// The drawer says what to do; the raw transport error stays in the log.
	$effect(() => { if (load?.state === 'error') console.warn('Could not load comments', load.error); });
	let listShown = false;
	$effect(() => {
		const id = focusedId;
		// Picking the selected tag again scrolls back to its card.
		void picks;
		const shown = isImage && surfaceState?.kind === 'list';
		const opening = shown && !listShown;
		listShown = shown;
		if (!shown || !id) return;
		void tick().then(() => scrollToCard(id, opening));
	});
	/**
	 * An image's drawer slides with the grid track its viewer animates, so it stays mounted while the track closes; with
	 * reduced motion it crossfades instead. A document's side drawer (a canvas wide enough for a column) appears and leaves
	 * at once; its sheet form uses AdaptiveDrawer's open/onClosed lifecycle.
	 */
	function drawerMotion(_node: Element, { leaving }: { leaving: boolean }) {
		if (!isImage) return { duration: 0 };
		const token = (name: string) => Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
		const pace = leaving ? 0.7 : 1;
		if (get(prefersReducedMotion)) return { duration: token('--dur-tight') * pace, css: (t: number) => `opacity: ${t}` };
		return { duration: leaving ? token('--dur-drawer') * pace : 0 };
	}
	function closeDrawer() { onClose(); restoreFocus(); }
	/** The card or thumbnail last clicked in the drawer: what an image covering the document was opened from. */
	let opener: Element | null = null;
	// Covered by the image a card opened, the drawer keeps its scroll; uncovered, that card has focus again.
	$effect.pre(() => {
		if (!covered) return;
		const back = openerFocus(opener, document.activeElement);
		return () => void tick().then(() => back?.isConnected && back.focus({ preventScroll: true }));
	});
	function navigate(id: string) {
		filterOpen = false;
		const comment = comments.find((item) => item.id === id);
		navigationNotice = comment?.orphaned || comment?.review ? 'This comment has no text to jump to. Its quote and discussion remain here.' : '';
		const useHalf = sheetDrawer && !comment?.regionNumber && !comment?.orphaned && !comment?.review;
		if (useHalf) detent = 'half';
		if (useHalf) onFocus(id, 'top');
		else onFocus(id);
	}
	const authorsWithOpenSuggestions = $derived.by(() => Object.entries(
		comments.filter((comment) => !comment.resolved && !comment.orphaned && comment.replacement !== null && comment.replacement !== undefined)
			.reduce<Record<string, DocComment[]>>((groups, comment) => ({ ...groups, [comment.author]: [...(groups[comment.author] ?? []), comment] }), {})
	).filter(([, suggestions]) => suggestions.length >= 2));
	// Whose accept-all is running. It disables every accept-all on purpose, across documents: one request at a time.
	let acceptAllBusy = $state<string | null>(null);
	const surfaceOpen = $derived(surfaceState !== null);
	// What one opening of the surface shows from its decisions: each card's failure (the surface makes the request, and a
	// card that moves groups remounts and would lose its own), the accept-all error, the skipped list and whose accept-all
	// it shows as running. A new document or opening replaces it; a decision writes only through `settle`, to the outcome
	// it started with, so nothing of it lands on what replaced it.
	type Outcome = { errors: Record<string, string>; acceptAllError: string; skipped: { id: string; reason: string }[]; acceptAllShownBusy: string | null };
	const fresh = (): Outcome => ({ errors: {}, acceptAllError: '', skipped: [], acceptAllShownBusy: null });
	let outcome = $state<Outcome>(fresh());
	function settle(started: Outcome, change: (current: Outcome) => void) { if (started === outcome) change(outcome); }
	// A refused accept (kind `stale`) goes to `suggestionRefusals` instead, which also shows its card as orphaned.
	const cardErrors = $derived({
		...(artifactId ? $suggestionRefusals[artifactId] : undefined),
		...Object.fromEntries(Object.entries(outcome.errors).filter(([, message]) => message)),
	});
	// The reset ends this document's refusals too: reopening the comments offers Accept again.
	$effect(() => {
		const artifact = artifactId; surfaceOpen;
		outcome = fresh();
		return () => { if (artifact) clearRefusals(artifact); };
	});
	const reason = (error: unknown) => (error instanceof Error ? error.message : String(error));
	async function submitSuggestionDecision(id: string, decision: 'accept' | 'reject', allFromAuthor = false) {
		// The document this decision is for. The props may name another one by the time it settles.
		const started = outcome, doc = target, artifact = artifactId;
		onSuggestionBusy?.(true, decision === 'accept' ? id : undefined);
		try {
			if (decision === 'accept') {
				if (!doc || !await flushDraft(doc)) throw new Error("Couldn't save the document before accepting this suggestion");
			}
			const result: SuggestionDecision = allFromAuthor
				? await decideSuggestion(id, decision, true)
				: await decideSuggestion(id, decision);
			settle(started, (current) => { current.skipped = result.skipped ?? []; });
			if (decision === 'accept' && result.text !== undefined && result.revision !== undefined && doc) {
				// The accepted text goes on screen only while that document's tab is still the one shown; closing the surface does not change that.
				const shown = target?.instanceId === doc.instanceId ? target : null;
				seedDraft(shown ?? doc, result.text, result.revision);
				if (shown) onSuggestionAccepted?.(result.text);
			}
			if (artifact) await loadComments(artifact);
		} catch (error) {
			// Bound to the document, like the accepted text. A decision made elsewhere comes back with a comments load; an
			// edit applied but not recorded is in the saved file, so the shown document is read again with its comments.
			const kind = error instanceof SuggestionRefused ? error.kind : null;
			if (kind === 'unrecorded' && doc && target?.instanceId === doc.instanceId) onDocumentChanged?.();
			else if ((kind === 'decided' || kind === 'unrecorded') && artifact) void loadComments(artifact).catch(() => undefined);
			throw error;
		} finally {
			onSuggestionBusy?.(false);
		}
	}
	async function decideOne(id: string, decision: 'accept' | 'reject') {
		const started = outcome, artifact = artifactId;
		started.errors = { ...started.errors, [id]: '' };
		try {
			await submitSuggestionDecision(id, decision);
			if (artifact) clearRefusal(artifact, id);
		} catch (error) {
			// The saved file no longer has the quote: the card stays orphaned with this reason until it is decided or the surface resets.
			const stale = decision === 'accept' && error instanceof SuggestionRefused && error.kind === 'stale';
			settle(started, (current) => {
				if (stale && artifact) recordRefusal(artifact, id, reason(error));
				else current.errors = { ...current.errors, [id]: reason(error) };
			});
		}
	}
	async function acceptAllFrom(author: string, id: string) {
		if (acceptAllBusy !== null) return;
		const started = outcome;
		acceptAllBusy = author; started.acceptAllShownBusy = author; started.acceptAllError = '';
		try {
			await submitSuggestionDecision(id, 'accept', true);
		} catch (error) {
			settle(started, (current) => { current.acceptAllError = reason(error); });
		} finally {
			acceptAllBusy = null;
			settle(started, (current) => { current.acceptAllShownBusy = null; });
		}
	}
</script>

{#snippet imageHead(row: unknown[])}
	{@const group = row as DocComment[]}
	{@const src = images?.srcOf(group[0])}
	{#if images && src}<ImageRegionThumb {src} alt={imageAlt(group[0])} caption comments={group} colorOf={images.colorOf} onOpen={onItsImage(group[0]) ? () => images?.onOpen(group[0]) : undefined} />{/if}
{/snippet}

{#snippet content()}
	{#if sheet.state?.kind === 'draft' && sheet.draft}<CommentComposer quote={sheet.draft.text} label={sheet.draft.label} body={sheet.draft.body ?? ''} disabledReason={draftReason} onBody={onDraftBody} onSubmit={onSubmit} onCancel={sheet.cancelDraft} />
	{:else}
		{#if !readOnly && sheet.state?.kind !== 'thread'}
			<div class="suggestion-status">
				{#each authorsWithOpenSuggestions as [author, suggestions] (author)}
					<button type="button" aria-busy={outcome.acceptAllShownBusy === author} disabled={acceptAllBusy !== null} onclick={() => acceptAllFrom(author, suggestions[0].id)}>{outcome.acceptAllShownBusy === author ? 'Accepting…' : `Accept all from ${author}`}</button>
				{/each}
				{#if outcome.skipped.length}<details><summary>{outcome.skipped.length} skipped</summary>{#each outcome.skipped as item (item.id)}<p>{item.reason}</p>{/each}</details>{/if}
			</div>
			{#if outcome.acceptAllError}<div class="suggestion-error" role="alert">{outcome.acceptAllError}</div>{/if}
		{/if}
		<CommentList bind:replyDrafts bind:replySelections leavingIds={held.leaving} reduced={$prefersReducedMotion} comments={displayed} documentOrder={sheet.state?.kind === 'list'} {focusedId} hoveredId={image?.hoveredId ?? null} onHover={image?.onHover} scrollOnFocus={!isImage} focusReply={isImage && sheet.state?.kind === 'thread'} showReply={shouldShowReply} keepReplyVisible={sheetDrawer && sheet.state?.kind === 'list'} {onViewed} onFocus={sheet.state?.kind === 'list' ? navigate : onFocus} {onReply} {onResolve} {onAgree} onDecision={decideOne} decisionErrors={cardErrors} tagColor={image?.colorOf ?? (images ? (comment) => images.colorOf(asDoc(comment)) : undefined)} onMovePin={image?.onMovePin ?? (images ? movePinInDocument : undefined)} groupOf={images ? (comment) => imageGroupKey(asDoc(comment)) : undefined} groupHead={images ? imageHead : undefined} {readOnly} reopenReachesOwner={load?.state === 'ready' && load.owner != null} />
	{/if}
{/snippet}

{#snippet listBody()}
	<div class="drawer-heading"><h2 tabindex="-1" data-drawer-focus-home>Comments <span>{headerCount}</span></h2><div class="drawer-tools"><div class="filter-menu">
		<button type="button" class="filter-button" data-drawer-filter aria-haspopup="menu" aria-expanded={filterOpen} aria-label="Filter comments: {filterName}" onclick={() => filterOpen = !filterOpen}>Filter <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg></button>
		{#if filterOpen}<div class="filter-popup" role="menu" aria-label="Filter comments" in:menuPop={{ origin: 'top right' }} out:menuPop={{ origin: 'top right' }}>{#each FILTERS as [key, name] (key)}<button type="button" role="menuitemradio" aria-checked={filter === key} onclick={() => { filter = key; filterOpen = false; }}><span class="filter-check" aria-hidden="true">{#if filter === key}<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5 9-10" /></svg>{/if}</span>{name}</button>{/each}</div>{/if}
	</div><IconButton ariaLabel="Close comments" variant="ghost" size="sm" onclick={closeDrawer}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg></IconButton></div></div>
	{#if load?.state === 'loading'}<p class="drawer-status">Loading comments…</p>{/if}
	{#if load?.state === 'error'}<div class="drawer-status" role="status">Couldn't load comments. Check the connection and retry.<Button size="sm" onclick={onRetry}>Retry</Button></div>{/if}
	{#if navigationNotice}<p class="drawer-status">{navigationNotice}</p>{/if}
	<div class="drawer-comments">
		{#if image && image.showThumbnail !== false && displayed.some((comment) => comment.region)}<ImageRegionThumb src={image.src} alt={image.alt} comments={displayed} colorOf={image.colorOf} onOpen={image.onOpen} />{/if}
		<div class="peers">{#key filter}<div class="peer" in:peerFade out:peerFade>
		{#if displayed.length}{@render content()}{:else if load?.state !== 'loading' && load?.state !== 'error'}<p class="drawer-status">{comments.length === 0 ? 'No comments yet.' : filter === 'open' ? 'No open comments.' : 'No resolved comments.'}{#if filter === 'resolved' && openCount}<button type="button" class="drawer-empty-action" onclick={() => filter = 'open'}>Show open · {openCount}</button>{/if}</p>{/if}
		</div>{/key}</div>
	</div>
	<div class="drawer-footers" use:drawerFooterHeight>
		{#if unsentText}<div class="drawer-unsent" data-drawer-unsent use:footerFocusHome={() => drawer?.querySelector<HTMLElement>('[data-drawer-focus-home]')}><p role="status">{unsentText.before}<span class="drawer-footer-owner" title={unsentText.owner}>{unsentText.owner}</span>{unsentText.after}</p><Button size="sm" onclick={() => void feedback?.send()} loading={feedback?.sending}>Send now</Button></div>{/if}
		<CommentPlacementRecovery {...placementRecovery} orphanedCount={placementRecovery.orphanedCount} onFocusHome={() => drawer?.querySelector<HTMLElement>('[data-drawer-focus-home]')} />
	</div>
{/snippet}

{#if surfaceState || sheet.closing}
	{#if sheet.state?.kind === 'list' && sheetDrawer}
		<AdaptiveDrawer ariaLabel="Document comments" headless fill opaque {detent} onDetent={(next) => detent = next} followKeyboard={narrow} within={column} {covered} open={sheet.open} onClose={sheet.close} onClosed={sheet.finishClose}><section class="canvas-comment-drawer sheet" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:listDrawerShown use:drawerKeys={closeDrawer} onclickcapture={(event) => opener = clickedOpener(event)}>{@render listBody()}</section></AdaptiveDrawer>
	{:else if surfaceState?.kind === 'list'}
		<section class="canvas-comment-drawer" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:listDrawerShown use:drawerKeys={closeDrawer} onclickcapture={(event) => opener = clickedOpener(event)} in:drawerMotion|global={{ leaving: false }} out:drawerMotion|global={{ leaving: true }}>{@render listBody()}</section>
	{:else if narrow && sheet.state}<AdaptiveDrawer ariaLabel="Document comments" heading={sheet.state.kind === 'draft' ? 'New comment' : 'Comments'} followKeyboard {covered} open={sheet.open} onClose={sheet.close} onClosed={sheet.finishClose}>{@render content()}</AdaptiveDrawer>
	{:else if surfaceState}<Popover anchor={cardAnchor} {bounds} avoidRect={image?.imageBounds} initialFocus={image && !readOnly ? '.ct-input' : undefined} open={true} {onClose} {restoreFocus}>{@render content()}</Popover>{/if}
{/if}

<style>
	.canvas-comment-drawer { grid-area: drawer; display: flex; flex-direction: column; gap: var(--space-1); min-height: 0; min-width: 0; padding: var(--space-1) var(--space-2) 0; border-top: 1px solid var(--border-subtle); }
	.canvas-comment-drawer.sheet { flex: 1; padding: 0; border-top: 0; }
	@container doc-canvas (min-width: 900px) { .canvas-comment-drawer { border-top: 0; border-left: 1px solid var(--border-subtle); padding-left: var(--space-3); } }
	.drawer-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 0 var(--space-1) 4px; }
	.drawer-heading h2 { margin: 0; font-size: var(--text-subhead); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	/* Where focus rests when Send now's footer takes it away: not a control, so Enter there does nothing. */
	.drawer-heading h2:focus { outline: none; }
	.drawer-heading h2:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
	.drawer-heading h2 span { color: var(--text-tertiary); font-size: var(--text-caption); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; margin-left: 2px; }
	/* The compact menu keeps the filter choices out of the thread list. */
	.drawer-tools { display: inline-flex; align-items: center; gap: var(--space-1); }
	.filter-menu { position: relative; }
	.filter-button, .filter-popup button { min-height: var(--space-6); border: 0; border-radius: var(--radius-xs); background: none; color: var(--text-secondary); font: inherit; font-size: var(--text-footnote); cursor: pointer; }
	.filter-button { display: inline-flex; align-items: center; gap: 4px; padding: 0 var(--space-2); }
	.filter-popup { position: absolute; z-index: 2; right: 0; top: calc(100% + var(--space-1)); display: grid; min-width: 9rem; padding: var(--space-1); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-elevated); box-shadow: var(--shadow-chip); }
	.filter-popup button { display: flex; align-items: center; gap: var(--space-1); padding: 0 var(--space-2); text-align: left; }
	.filter-check { display: inline-flex; flex: none; width: 12px; height: 12px; }
	.filter-popup button[aria-checked="true"] { color: var(--accent); font-weight: var(--fw-semibold); }
	@media (pointer: coarse) { .drawer-heading :global(button), .filter-button, .filter-popup button { min-height: 44px; } }
	/* A filter switch crossfades: the leaving and arriving lists share one grid cell, so neither moves the other. */
	.peers { display: grid; }
	.peer { grid-area: 1 / 1; min-width: 0; }
	.drawer-comments { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
	.drawer-status { color: var(--text-secondary); font-size: var(--text-footnote); margin: 4px 0; }
	.drawer-empty-action { margin-left: var(--space-1); padding: 0; border: 0; background: none; color: var(--accent); font: inherit; font-weight: var(--fw-semibold); cursor: pointer; }
	.drawer-empty-action:hover { text-decoration: underline; }
	/* The one measured wrapper owns the drawer's pinned-footer height, including either or both child footers. */
	.drawer-footers { flex: none; }
	.drawer-unsent { container: drawer-footer / inline-size; display: flex; align-items: center; gap: var(--space-2); margin: 0; padding: var(--space-2) var(--space-3); border-top: 1px solid var(--border-subtle); border-radius: 0 0 calc(var(--radius-lg) - 1px) calc(var(--radius-lg) - 1px); background: var(--bg-secondary); }
	.drawer-unsent p { flex: 1; min-width: 0; margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); overflow-wrap: anywhere; }
	/* A long owner name is cut only where the footer is too narrow to hold it on one line of the sentence. */
	@container drawer-footer (max-width: 30rem) { .drawer-footer-owner { display: inline-block; max-width: 14ch; overflow: hidden; text-overflow: ellipsis; vertical-align: bottom; white-space: nowrap; } }
	.drawer-unsent :global(.btn) { flex: none; }
	@media (max-width: 420px) { .drawer-unsent { gap: var(--space-1); padding-inline: var(--space-2); } }
	@media (pointer: coarse) { .drawer-unsent :global(.btn) { min-height: 44px; } }
	.suggestion-status { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
	.suggestion-status button { background: none; border: none; padding: 0; font: inherit; font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--accent); cursor: pointer; }
	.suggestion-status button:not(:disabled):hover { text-decoration: underline; }
	.suggestion-status button:disabled { opacity: 0.55; cursor: not-allowed; }
	.suggestion-status button:disabled[aria-busy="true"] { color: var(--text-primary); background: var(--accent-bg); border-radius: var(--radius-xs); padding: 2px 6px; margin: -2px -6px; opacity: 1; }
	.suggestion-error { font-size: var(--text-caption); color: var(--red-text); }
	.suggestion-status details { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.suggestion-status p { margin: 4px 0; }
</style>
