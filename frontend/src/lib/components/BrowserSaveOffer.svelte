<script lang="ts">
	import { tick, untrack } from 'svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { declineOffer, fetchSaveOffer, saveOffer, type SaveOffer } from '$lib/api/savedLoginOffer';
	import { SAVE_OFFER_TEXT as text, shortenMiddle } from '$lib/browserSaveOfferText';
	import { CONFIRMATION_HOLD_MS } from '$lib/confirmationHold';
	import { ms } from '$lib/popoverMotion';

	/**
	 * The offer to save a login the person just made in this pane, docked below the page.
	 * It asks the server only after the person typed or pasted into a password page, never polls,
	 * and never shows or asks for a password.
	 */
	let { threadId, passwordPage, onopenchange, onrestorefocus }: {
		threadId: string;
		passwordPage: boolean;
		/** Called as the dock opens or closes, so the host can keep the agent's viewport still. */
		onopenchange: (open: boolean) => void;
		/** Called while focus is still in the dock, so it lands on the page and not on a removed button. */
		onrestorefocus: () => void;
	} = $props();

	// The "Saved" line stays as long as the app's other confirmations ("Copied") do, then fades over the standard motion duration.
	// The close timer reads that token from the document, the same one the stylesheet's transition uses, so the two cannot drift apart.
	const FADE_DURATION_VAR = '--dur-standard';

	let offer = $state<SaveOffer | null>(null);
	let phase = $state<'asking' | 'saved' | 'fading'>('asking');
	let failed = $state(false);
	let saving = $state(false);
	let announcement = $state('');
	let lockedHeight = $state(0);
	let dock = $state<HTMLElement | undefined>();
	let saveButton = $state<HTMLButtonElement | undefined>();
	let typedInPasswordPage = false;
	// The server's confirmation can land a moment after the page settles, so one empty answer earns one more ask.
	let askAgain = false;
	let generation = 0;
	let timers: ReturnType<typeof setTimeout>[] = [];
	let wasPasswordPage = false;

	// The whole domain and username, until they would not fit: the question in two lines, the username in one.
	// Each is measured in a hidden copy with the same width and type, so the visible one is set once.
	let domain = $state('');
	let username = $state('');
	let probe = $state<HTMLElement | undefined>();
	let probeDomain = $state<HTMLElement | undefined>();
	let nameProbe = $state<HTMLElement | undefined>();
	let nameProbeText = $state<HTMLElement | undefined>();
	const probeParts = $derived(offer ? text.question(offer, '\u0001').split('\u0001') : ['', '']);

	function lineCount(node: HTMLElement) {
		const style = getComputedStyle(node);
		return Math.round(node.getBoundingClientRect().height / (parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.4 || 20));
	}

	/** The longest middle-shortened form of `whole` whose probe holds at most `lines` lines. */
	function fit(whole: string, probeBox: HTMLElement, probeText: HTMLElement, lines: number) {
		const fits = (shown: string) => { probeText.textContent = shown; return lineCount(probeBox) <= lines; };
		if (fits(whole)) return whole;
		let low = 1, high = whole.length - 1;
		while (low < high) {
			const mid = Math.ceil((low + high) / 2);
			if (fits(shortenMiddle(whole, mid))) low = mid; else high = mid - 1;
		}
		return shortenMiddle(whole, low);
	}

	function fitText() {
		if (!offer || !probe || !probeDomain || !nameProbe || !nameProbeText) return;
		domain = fit(offer.domain, probe, probeDomain, 2);
		username = fit(offer.username, nameProbe, nameProbeText, 1);
	}

	$effect(() => {
		if (!dock || !probe || !probeDomain || !nameProbe || !nameProbeText || !offer) return;
		let width = -1;
		const observer = new ResizeObserver((entries) => {
			const next = Math.round(entries[0]?.contentRect.width ?? 0);
			if (next !== width) { width = next; fitText(); }
		});
		observer.observe(dock);
		untrack(fitText);
		return () => observer.disconnect();
	});

	async function ask(retry = false) {
		const mine = generation;
		const next = await fetchSaveOffer(threadId);
		if (mine !== generation) return;
		askAgain = next === null && !retry;
		if (!next || offer) return;
		offer = next;
		domain = next.domain;
		username = next.username;
		phase = 'asking';
		failed = false;
		saving = false;
		announcement = text.announce;
		onopenchange(true);
	}

	function close() {
		if (dock?.contains(document.activeElement)) onrestorefocus();
		timers.forEach(clearTimeout);
		timers = [];
		const wasOpen = offer !== null;
		offer = null;
		saving = false;
		failed = false;
		lockedHeight = 0;
		if (wasOpen) onopenchange(false);
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

	/** A navigation settled; a page on another site ends the offer, and an earlier empty answer is asked once more. */
	export function navigated(nextDomain: string | null) {
		if (offer && nextDomain !== null && nextDomain !== offer.domain) { close(); return; }
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
		failed = false;
		const mine = generation;
		const result = await saveOffer(threadId);
		if (mine !== generation || !offer) return;
		saving = false;
		if (result === 'gone') { close(); return; }
		if (result === 'error') {
			failed = true;
			announcement = text.error;
			// The button was disabled while saving; keyboard focus comes back to it.
			void tick().then(() => saveButton?.focus());
			return;
		}
		// The buttons are about to go, so focus moves to the page first.
		onrestorefocus();
		lockedHeight = dock?.offsetHeight ?? 0;
		phase = 'saved';
		announcement = offer.update ? text.updated : text.saved;
		finishAfterHold();
	}

	function finishAfterHold() {
		const reduced = $prefersReducedMotion;
		timers.push(setTimeout(() => {
			if (reduced) { close(); return; }
			phase = 'fading';
			timers.push(setTimeout(close, ms(FADE_DURATION_VAR)));
		}, CONFIRMATION_HOLD_MS));
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
	<div bind:this={dock} class="save-offer" class:fading={phase === 'fading'} style:min-height={lockedHeight ? `${lockedHeight}px` : undefined}>
		{#if phase === 'asking'}
			<div class="body">
			<div class="copy">
				<p class="probe" aria-hidden="true" bind:this={probe}>{probeParts[0]}<span bind:this={probeDomain}></span>{probeParts[1]}</p>
				<p class="question" aria-hidden="true">{text.question(offer, domain)}</p>
				<p class="sr-only">{text.question(offer, offer.domain)}</p>
				<p class="username probe" aria-hidden="true" bind:this={nameProbe}><span bind:this={nameProbeText}></span></p>
				<p class="username" title={offer.username}><span aria-hidden="true">{username}</span><span class="sr-only">{offer.username}</span></p>
				{#if failed}<p class="error">{text.error}</p>{/if}
			</div>
			<div class="actions">
				<Button variant="primary" loading={saving} bind:el={saveButton} onclick={() => void save()}>{offer.update ? text.update : text.save}</Button>
				<Button variant="ghost" onclick={notNow}>{text.notNow}</Button>
			</div>
			</div>
		{:else}
			<p class="done" aria-hidden="true">{offer.update ? text.updated : text.saved}</p>
		{/if}
	</div>
{/if}

<style>
	.save-offer { container-type: inline-size; flex: none; border-top: 1px solid var(--border-subtle); background: var(--bg-primary); transition: opacity var(--dur-standard) ease; }
	.save-offer.fading { opacity: 0; }
	.body { display: flex; flex-direction: column; justify-content: center; gap: var(--space-2); padding: var(--space-3) var(--space-4); }
	.copy { position: relative; display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
	p { margin: 0; }
	.question, .probe { overflow-wrap: anywhere; font-size: var(--text-subhead); line-height: 1.4; color: var(--text-primary); }
	.probe { position: absolute; inset-inline: 0; top: 0; visibility: hidden; pointer-events: none; }
	.username { overflow-wrap: anywhere; font-size: var(--text-footnote); line-height: 1.4; color: var(--text-secondary); }
	.error { font-size: var(--text-footnote); color: var(--red-text); }
	.actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
	.done { padding: var(--space-3) var(--space-4); font-size: var(--text-subhead); color: var(--text-secondary); }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
	/* Wide enough, the actions sit on the right of the question and username; narrower, they stack below. */
	@container (min-width: 36em) {
		.body { flex-direction: row; align-items: center; justify-content: space-between; gap: var(--space-4); }
		.copy { flex: 1; }
		.actions { flex: none; }
	}
	@media (max-width: 48em) { .actions :global(.btn) { height: var(--touch-target); } }
	@media (prefers-reduced-motion: reduce) { .save-offer { transition: none; } }
</style>
