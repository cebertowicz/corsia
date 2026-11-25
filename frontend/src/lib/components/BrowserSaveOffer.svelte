<script lang="ts">
	import { tick, untrack } from 'svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { declineOffer, fetchSaveOffer, saveOffer, type SaveOffer } from '$lib/api/savedLoginOffer';
	import { hostInDomain } from '$lib/browserAddress';
	import { SAVE_OFFER_TEXT as text, shortenMiddle } from '$lib/browserSaveOfferText';
	import { CONFIRMATION_HOLD_MS } from '$lib/confirmationHold';
	import { cssVar, ms } from '$lib/popoverMotion';
	import { blockSlot } from '$lib/slotMotion';

	/**
	 * The offer to save a login the person just made in this pane, laid over the bottom edge of the page, never resizing it.
	 * It asks the server only after the person typed or pasted into a password page, never polls,
	 * and never shows or asks for a password.
	 */
	let { threadId, passwordPage, onrestorefocus }: {
		threadId: string;
		passwordPage: boolean;
		/** Called while focus is still in the dock, so it lands on the page and not on a removed button. */
		onrestorefocus: () => void;
	} = $props();

	// The "Saved" line stays as long as the app's other confirmations ("Copied") do, then fades over the standard motion duration.
	// The close timer reads that token from the document, the same one the stylesheet's transition uses, so the two cannot drift apart.
	const FADE_DURATION_VAR = '--dur-standard';

	let offer = $state<SaveOffer | null>(null);
	let phase = $state<'asking' | 'saved' | 'fading'>('asking');
	// The dock keeps the height it had while it asked, so swapping the question for "Saved" changes no size (motion.md: a content swap has no size jump).
	// It is a floor and not a fixed height: a pane that narrows while "Saved" shows grows the box instead of clipping the line.
	let heldHeight = $state<string | null>(null);
	// The question is the dock's accessible name, so a Tab-reached button is announced with it. One id per instance, as every named region in the app has.
	const instanceId = $props.id();
	const labelId = `save-offer-${instanceId}`;
	// The pane's own error line, for a failure the person can retry.
	let failure = $state<string | null>(null);
	// The server's sentence for a refusal that cannot succeed (a 404 or a 409): it takes the question's place, Save goes, and Close stays.
	let refusal = $state<string | null>(null);
	let saving = $state(false);
	let announcement = $state('');
	let dock = $state<HTMLElement | undefined>();
	let saveButton = $state<HTMLButtonElement | undefined>();
	let closeButton = $state<HTMLButtonElement | undefined>();
	let typedInPasswordPage = false;
	// The server's confirmation can land a moment after the page settles, so one empty answer earns one more ask.
	let askAgain = false;
	let generation = 0;
	let timers: ReturnType<typeof setTimeout>[] = [];
	let wasPasswordPage = false;

	// The whole username, until it would not fit its line: then it is cut in the middle, measured in a hidden copy with the same width and type.
	// The domain is never cut; the question wraps around it.
	let username = $state('');
	let nameProbe = $state<HTMLElement | undefined>();
	let nameProbeText = $state<HTMLElement | undefined>();

	function fitUsername() {
		if (!offer || !nameProbe || !nameProbeText) return;
		const fits = (shown: string) => { nameProbeText!.textContent = shown; return nameProbe!.scrollWidth <= nameProbe!.clientWidth; };
		const whole = offer.username;
		if (fits(whole)) { username = whole; return; }
		let low = 1, high = whole.length - 1;
		while (low < high) {
			const mid = Math.ceil((low + high) / 2);
			if (fits(shortenMiddle(whole, mid))) low = mid; else high = mid - 1;
		}
		username = shortenMiddle(whole, low);
	}

	$effect(() => {
		if (!dock || !nameProbe || !nameProbeText || !offer) return;
		let width = -1;
		const observer = new ResizeObserver((entries) => {
			const next = Math.round(entries[0]?.contentRect.width ?? 0);
			if (next !== width) { width = next; fitUsername(); }
		});
		observer.observe(dock);
		untrack(fitUsername);
		return () => observer.disconnect();
	});

	async function ask(retry = false) {
		const mine = generation;
		const next = await fetchSaveOffer(threadId);
		if (mine !== generation) return;
		askAgain = next === null && !retry;
		if (!next || offer) return;
		offer = next;
		username = next.username;
		phase = 'asking';
		heldHeight = null;
		failure = null;
		refusal = null;
		saving = false;
		announcement = text.announce(next);
	}

	function close() {
		if (dock?.contains(document.activeElement)) onrestorefocus();
		timers.forEach(clearTimeout);
		timers = [];
		offer = null;
		saving = false;
		failure = null;
		refusal = null;
	}

	/** Forget everything about this stream: a new thread, a reconnect or a closed pane starts clean. */
	export function reset() {
		generation++;
		typedInPasswordPage = false;
		askAgain = false;
		announcement = '';
		close();
	}

	/** The person typed or pasted in this pane; only input on a password page counts toward an offer. */
	export function typed() {
		if (passwordPage) typedInPasswordPage = true;
	}

	/**
	 * A navigation settled; a page on another site ends the offer, and an earlier empty answer is asked once more.
	 * The pane names the registrable domain, the offer the full host it saves for (a subdomain, perhaps a port).
	 */
	export function navigated(nextDomain: string | null) {
		if (offer && nextDomain !== null && !hostInDomain(offer.domain, nextDomain)) { close(); return; }
		if (!offer && askAgain && nextDomain !== null) { askAgain = false; void ask(true); }
	}

	$effect(() => {
		const visible = passwordPage;
		untrack(() => {
			if (wasPasswordPage && !visible && typedInPasswordPage) void ask();
			if (wasPasswordPage && !visible) typedInPasswordPage = false;
			wasPasswordPage = visible;
		});
	});

	let lastThread = untrack(() => threadId);
	$effect(() => {
		const id = threadId;
		if (id === lastThread) return;
		lastThread = id;
		untrack(reset);
	});

	$effect(() => () => untrack(reset));

	async function save() {
		if (!offer || saving) return;
		saving = true;
		failure = null;
		// Emptied while the save runs, so the same failure line twice in a row is still a change the live region reads out.
		announcement = '';
		const mine = generation;
		const result = await saveOffer(threadId);
		if (mine !== generation || !offer) return;
		saving = false;
		// Save was disabled while it ran, so focus may have dropped to the document. Focus moves only if it is still in the
		// dock or on the document: a person who went on to type somewhere else (the chat, say) keeps their place.
		const focusWasHere = !document.activeElement || document.activeElement === document.body || !!dock?.contains(document.activeElement);
		if (!result.saved && result.message) {
			// Save is about to go, so focus moves to Close.
			showRefusal(result.message);
			announcement = result.message;
			if (focusWasHere) void tick().then(() => closeButton?.focus());
			return;
		}
		if (!result.saved) {
			failure = text.error;
			announcement = failure;
			if (focusWasHere) void tick().then(() => saveButton?.focus());
			return;
		}
		// The buttons are about to go, so focus moves to the page first.
		if (focusWasHere) onrestorefocus();
		holdHeight();
		phase = 'saved';
		announcement = offer.update ? text.updated : text.saved;
		finishAfterHold();
	}

	/** Reads the dock's rendered height while it still asks, and keeps it as the dock's floor for the confirmation, its fade and a refusal. */
	function holdHeight() {
		const measured = dock?.getBoundingClientRect().height ?? 0;
		heldHeight = measured > 0 ? `${measured}px` : null;
		return measured;
	}

	/**
	 * The refusal takes the question's place at no less than the asking height. A sentence that needs more room eases the extra
	 * height in over the standard duration; under reduced motion the size changes at once and the content crossfades instead.
	 */
	function showRefusal(message: string) {
		const from = holdHeight();
		refusal = message;
		void tick().then(() => {
			if (!dock || refusal !== message || from <= 0) return;
			const to = dock.getBoundingClientRect().height;
			if (to - from < 0.5) return;
			if ($prefersReducedMotion) {
				dock.querySelector('.body')?.animate([{ opacity: 0 }, { opacity: 1 }], { duration: ms('--dur-tight'), easing: cssVar('--ease-snap') });
				return;
			}
			const node = dock;
			node.style.overflow = 'hidden';
			const grow = node.animate([{ height: `${from}px` }, { height: `${to}px` }], { duration: ms('--dur-standard'), easing: cssVar('--ease-snap') });
			const release = () => node.style.removeProperty('overflow');
			void grow.finished.then(release, release);
		});
	}

	function finishAfterHold() {
		const reduced = $prefersReducedMotion;
		timers.push(setTimeout(() => {
			if (reduced) { close(); return; }
			phase = 'fading';
			timers.push(setTimeout(close, ms(FADE_DURATION_VAR)));
		}, CONFIRMATION_HOLD_MS));
	}

	/** On leaving the dock only fades, at 0.7 of the standard duration (the reduced-motion crossfade runs over the tight one); its arrival is the stylesheet's. */
	function leave(node: Element) {
		const duration = ms($prefersReducedMotion ? '--dur-tight' : '--dur-standard') * 0.7;
		// A real 0 stays 0: the Saved line has already faded out by the time it closes, so it must not fade a second time from full.
		const parsed = Number.parseFloat(getComputedStyle(node).opacity);
		const from = Number.isFinite(parsed) ? parsed : 1;
		return { duration, css: (t: number) => `opacity: ${t * from}` };
	}

	function notNow() {
		if (!offer) return;
		onrestorefocus();
		void declineOffer(threadId);
		close();
	}
</script>

<span class="sr-only" role="status" aria-live="polite">{announcement}</span>
{#if offer}
	<div
		bind:this={dock}
		class="save-offer"
		class:fading={phase === 'fading'}
		aria-busy={saving || undefined}
		role="group"
		aria-labelledby={phase === 'asking' ? labelId : undefined}
		style:min-height={heldHeight}
		out:leave
	>
		{#if phase === 'asking'}
			<div class="body">
				<div class="copy">
					<p class="question" id={labelId}>{refusal ?? text.question(offer, offer.domain)}</p>
					{#if !refusal}
						<p class="username probe" aria-hidden="true" bind:this={nameProbe}><span bind:this={nameProbeText}></span></p>
						<p class="username" title={offer.username}><span aria-hidden="true">{username}</span><span class="sr-only">{offer.username}</span></p>
					{/if}
					{#if failure}<p class="error" in:blockSlot out:blockSlot>{failure}</p>{/if}
				</div>
				<div class="actions">
					{#if refusal}
						<Button variant="ghost" bind:el={closeButton} onclick={notNow}>{text.close}</Button>
					{:else}
						<span class="save"><Button variant="primary" loading={saving} bind:el={saveButton} onclick={() => void save()}>{offer.update ? text.update : text.save}</Button></span>
						<Button variant="ghost" onclick={notNow}>{text.notNow}</Button>
					{/if}
				</div>
			</div>
		{:else}
			<p class="done" aria-hidden="true">{offer.update ? text.updated : text.saved}</p>
		{/if}
	</div>
{/if}

<style>
	/* The docked card of the document review bar (.rb-dock): over the page's bottom edge, solid, top corners rounded, and the same hairline on its top and sides, so its edge shows on a white page. */
	.save-offer { position: absolute; z-index: 2; left: 0; right: 0; bottom: 0; box-sizing: border-box; container-type: inline-size; display: flex; flex-direction: column; justify-content: center; border: 1px solid var(--border-subtle); border-bottom: 0; border-radius: var(--radius-xl) var(--radius-xl) 0 0; background: var(--bg-secondary); box-shadow: var(--glass-elevation-1); animation: dock-in var(--dur-standard) var(--ease-entrance) backwards; transition: opacity var(--dur-standard) var(--ease-snap); }
	.save-offer.fading { opacity: 0; }
	@keyframes dock-in { from { opacity: 0; transform: translateY(var(--space-4)); } }
	@keyframes dock-fade { from { opacity: 0; } }
	.body { display: flex; flex-direction: column; justify-content: center; gap: var(--space-2); padding: var(--space-3) var(--space-4) calc(var(--space-3) + var(--safe-bottom, 0px)); }
	.copy { position: relative; display: flex; flex-direction: column; min-width: 0; }
	p { margin: 0; }
	.question { overflow-wrap: anywhere; font-size: var(--text-subhead); line-height: 1.4; color: var(--text-primary); }
	.username { margin-top: var(--space-1); overflow: hidden; white-space: nowrap; font-size: var(--text-footnote); line-height: 1.4; color: var(--text-secondary); }
	.probe { position: absolute; inset-inline: 0; top: 0; margin: 0; visibility: hidden; pointer-events: none; }
	.error { margin-top: var(--space-1); font-size: var(--text-footnote); color: var(--red-text); }
	/* Save and Not now sit right-aligned as a pair. They do different things, so at least a target's width of room sits between them. */
	.actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; margin-left: auto; gap: var(--space-2) calc(var(--space-8) * 2); }
	/* While saving the spinner takes the label's place, so Save keeps its width and Not now stays under the person's thumb. */
	.save { display: inline-flex; }
	.save :global(.btn.loading) { color: transparent; }
	.save :global(.spinner) { position: absolute; inset: 0; margin: auto; border-color: var(--accent-on-fill); border-right-color: transparent; }
	.actions :global(.btn:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* The confirmation line takes the body's padding, and the dock holds it in the middle of the height the question had. */
	.done { padding: var(--space-3) var(--space-4) calc(var(--space-3) + var(--safe-bottom, 0px)); font-size: var(--text-subhead); color: var(--text-secondary); }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
	/* Wide enough, the pair sits on the right of the question and username; narrower, it takes its own row, still on the right. */
	@container (min-width: 36em) {
		.body { flex-direction: row; align-items: center; gap: var(--space-4); }
		.copy { flex: 1; }
		.actions { flex: none; }
	}
	@media (max-width: 48em), (pointer: coarse) { .actions :global(.btn) { height: var(--touch-target); } }
	/* Reduced motion keeps a crossfade rather than an instant swap, which is what motion.md asks for a content change. The app's reset sets every animation to 0.01ms with !important, so the tight duration has to out-rank it. */
	@media (prefers-reduced-motion: reduce) { .save-offer { animation-name: dock-fade; animation-duration: var(--dur-tight) !important; animation-timing-function: var(--ease-snap); } }
</style>
