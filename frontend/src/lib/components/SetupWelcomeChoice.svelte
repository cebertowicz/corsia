<script lang="ts">
	import { Button, Popover, RailRow, Spinner, Toggle } from '$lib/components/ui';
	import { ANCHOR_MOVED } from '$lib/components/ui/Popover.svelte';
	import { onMount } from 'svelte';
	import SetupSubscriptionQuestion, { type SetupPicks } from './SetupSubscriptionQuestion.svelte';
	import { needsSpace, noLocalModel, type LocalPick } from '$lib/setupLocalPick.svelte';
	import { welcomeModelMeta } from '$lib/setupLocalPresentation';
	import { MANAGE_DISCLOSURE, MANAGE_DISCLOSURE_LEAD, MANAGE_LABEL, MANAGE_META, OWN_OMLX_FOUND, TERMS_LABEL, USE_MINE_LABEL, USE_MINE_META } from '$lib/ownOmlxCopy';

	interface Props {
		localEligible?: boolean;
		freeDiskGb?: number;
		pick: LocalPick | null;
		downloadEtaSeconds?: number | null;
		downloadStalled?: boolean;
		wanted: boolean;
		onWantedChange: (next: boolean) => void;
		onRecheck: () => void;
		onRetry: () => void;
		/** The person runs their own oMLX (J9): both choices use it; `manage` is Let CORSIA manage it. */
		ownOmlx?: boolean;
		manage?: boolean;
		onManageChange?: (next: boolean) => void;
		/** Which subscriptions or key the person said they pay for. */
		picks: SetupPicks;
		onTogglePick: (key: keyof SetupPicks) => void;
		/** Claude Code or Codex is already ready (its name), so the question is not asked. */
		ready?: string | null;
		/** The question waits while CORSIA checks whether either is installed. */
		detecting?: boolean;
	}
	const { localEligible, freeDiskGb, pick, downloadEtaSeconds, downloadStalled, wanted, onWantedChange, onRecheck, onRetry, ownOmlx = false, manage = true, onManageChange, picks, onTogglePick, ready = null, detecting = false }: Props = $props();
	/** Something will run chat besides the local model: a tool already ready, or one the person picked. */
	const hasSource = $derived(Boolean(ready) || picks.claude || picks.chatgpt || picks.key);
	/** Until one is picked the local model is the only way to chat, so its switch stays on. */
	const locked = $derived(!hasSource);
	/** A short window (the setup floor) packs the choices tighter so the first one shows above the dock. */
	let short = $state(false);
	/** oMLX's terms sit behind one control at every window size, and open in a popover. */
	let termsOpen = $state(false);
	let termsButton = $state<HTMLButtonElement>();
	let choices = $state<HTMLElement>();
	let choicesWidth = $state(0);
	function toggleTerms() {
		choicesWidth = choices?.getBoundingClientRect().width ?? 0;
		termsOpen = !termsOpen;
	}
	/** Extra height for the card, so that when it opens upward its top edge ends between two lines of the page. */
	let extra = $state(0);
	/** The card spans the column of choices, so it covers whole lines and never leaves a few words showing beside it. */
	const popoverAnchor = () => {
		const column = choices?.getBoundingClientRect();
		const button = termsButton?.getBoundingClientRect();
		return column && button ? new DOMRect(column.left, button.top, column.width, button.height) : null;
	};
	/**
	 * Where the card lands is measured, not guessed: opened upward (no room below, as on the setup floor), its bottom already sits in
	 * the gap above the control; if its top would cut a line, the card grows by that part of a line, so both edges fall between lines.
	 */
	let growths = 0;
	function clearTopEdge(card: HTMLElement) {
		const body = card.querySelector<HTMLElement>('.disclose');
		const button = termsButton?.getBoundingClientRect();
		// Its own style says where it is going: the card may still be sliding there, so its box now is not where it will rest.
		const left = parseFloat(card.style.left), top0 = parseFloat(card.style.top);
		const edge = { left, right: left + card.offsetWidth, top: top0, bottom: top0 + card.offsetHeight };
		if (!body || !button || edge.bottom > button.top || growths >= 3) return;
		let top = edge.top;
		const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
		for (let node = walker.nextNode(); node; node = walker.nextNode()) {
			if (!node.textContent?.trim() || card.contains(node)) continue;
			const range = document.createRange();
			range.selectNodeContents(node);
			for (const line of range.getClientRects()) {
				if (line.height >= 4 && line.right > edge.left && line.left < edge.right && line.top < edge.top - 1 && line.bottom > edge.top + 1) top = Math.min(top, line.top);
			}
		}
		if (top === edge.top) return;
		growths += 1;
		extra = body.offsetHeight + (edge.top - top);
		window.dispatchEvent(new Event(ANCHOR_MOVED));
	}
	$effect(() => { if (!termsOpen) { growths = 0; extra = 0; } });
	onMount(() => {
		const query = window.matchMedia('(max-height: 700px)');
		const sync = () => { short = query.matches; };
		sync();
		query.addEventListener('change', sync);
		return () => query.removeEventListener('change', sync);
	});
	// Nothing fits is told the same way as too little memory: there is no local model for this computer.
	const noModel = $derived(noLocalModel(localEligible, pick));
	const modelSize = $derived(pick?.model ? `${pick.model.download_size_gb} GB` : '');
</script>

<div class="welcome-copy" class:tight={ownOmlx && short}>
	<SetupSubscriptionQuestion {picks} onToggle={onTogglePick} {ready} {detecting} />
	{#if noModel}
		{#if !hasSource && !detecting}<p>This computer doesn’t have enough memory to run a local model, so pick at least one above.</p>{/if}
	{:else if needsSpace(pick, freeDiskGb)}
		<p>Needs {modelSize} free; {freeDiskGb} GB is free.</p>
		<Button variant="chip" onclick={onRecheck}>Free up space, then check again</Button>
	{:else}
		<div class="download-choice">
			<span class="switch"><Toggle checked={wanted || locked} {locked} onchange={onWantedChange} ariaLabel="Also run a local model on this computer" describedBy={locked && !detecting ? "local-locked-why" : undefined} /></span>
			<div class="choice-copy">
				<span class="choice-name">Also run a local model on this computer</span>
				<span class="why">Free, private, and works offline. It’s slower than Claude or ChatGPT, so it’s for work you’d rather keep on this computer, and you can move background jobs onto it.</span>
				{#if locked && !detecting}
					<span class="spec" id="local-locked-why">On until you pick one above.</span>
				{/if}
				{#if !wanted && !locked && !ownOmlx}
					<span class="spec">Off. You can turn it on later in Settings.</span>
				{:else if ownOmlx}
					<span class="spec">{OWN_OMLX_FOUND}</span>
				{:else if pick?.state === 'checking'}
					<span class="spec checking"><Spinner size={12} />Finding the model that fits this computer…</span>
				{:else if pick?.state === 'failed'}
					<span class="spec">CORSIA couldn’t check which local AI model fits this computer.</span>
					<span class="spec">You can go on and add a local model from the app later.</span>
					<span class="retry"><Button variant="chip" size="sm" onclick={onRetry}>Try again</Button></span>
				{:else if pick?.model}
					<span class="spec">{welcomeModelMeta(pick.model.name, modelSize, downloadEtaSeconds, downloadStalled)}</span>
				{/if}
				{#if pick?.state !== 'failed' && !ownOmlx && (wanted || locked)}
					<span class="spec">The download starts in the background when you press Get started.</span>
				{/if}
			</div>
		</div>
		{#if ownOmlx && wanted}
			<div class="picks" role="radiogroup" aria-label="Your oMLX" bind:this={choices}>
				<RailRow pick="one" checked={manage} label={MANAGE_LABEL} meta={MANAGE_META} onclick={() => onManageChange?.(true)} />
				<RailRow pick="one" checked={!manage} label={USE_MINE_LABEL} meta={USE_MINE_META} onclick={() => onManageChange?.(false)} />
			</div>
			<span class="terms"><Button variant="chip" size="sm" bind:el={termsButton} onclick={toggleTerms}>{TERMS_LABEL}</Button></span>
			<Popover anchor={popoverAnchor} onPlace={clearTopEdge} width={choicesWidth ? `${choicesWidth}px` : undefined} open={termsOpen} onClose={() => (termsOpen = false)} restoreFocus={() => termsButton?.focus()} placement="bottom">
				<p class="disclose pop" style:min-height={extra ? `${extra}px` : undefined}><strong>{MANAGE_DISCLOSURE_LEAD}</strong> {MANAGE_DISCLOSURE}</p>
			</Popover>
		{/if}
	{/if}
</div>

<style>
	.welcome-copy { display: flex; flex-direction: column; gap: var(--space-3); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); }
	/* The floor with their own oMLX has two more rows than the window holds: tighter rows let the first choice show above the dock. */
	.welcome-copy.tight { gap: var(--space-1); }
	.welcome-copy.tight .choice-copy { gap: 0; }
	.welcome-copy p { margin: 0; }
	.download-choice { display: flex; align-items: flex-start; gap: var(--space-3); }
	.switch { flex: none; display: inline-flex; padding-top: var(--space-1); }
	.choice-copy { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
	.choice-name { color: var(--text-primary); font-size: var(--text-body); font-weight: var(--fw-medium); }
	.why { color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); text-wrap: pretty; }
	.spec { color: var(--text-tertiary); font-size: var(--text-footnote); line-height: var(--lh-normal); text-wrap: pretty; }
	.checking { display: inline-flex; align-items: center; gap: var(--space-2); }
	.retry { align-self: flex-start; padding-top: var(--space-1); }
	.picks { display: flex; flex-direction: column; gap: 1px; margin-left: calc(var(--space-2) * -1); }
	/* One collapsed control under the choices; its card opens above or below it, wherever it fits whole. */
	.terms { align-self: flex-start; display: inline-flex; }
	.disclose.pop { padding: var(--space-3); }
	.disclose { margin: 0; font-size: var(--text-footnote); color: var(--text-secondary); line-height: var(--lh-normal); text-wrap: pretty; }
	.disclose strong { font-weight: var(--fw-medium); color: var(--text-primary); }
</style>
