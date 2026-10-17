<script lang="ts">
	/**
	 * The canonical decision chip — one renderer for one persisted decision
	 * identity, wherever the dock is mounted.
	 *
	 * It owns only the unsent draft plus one durable retry key; the server owns
	 * content, options, order, and revision. The draft is keyed by decision
	 * identity in `$lib/stores/attentionDock`, so a route switch cannot discard
	 * what the user typed. Sending hands off to that store: the dock advances at
	 * once, and a failed write returns the decision here with its reason.
	 */
	import Button from '$lib/components/ui/Button.svelte';
	// The Other / Note row is a <label>, so its textarea cannot sit inside Field: it takes Field's shared `.text-field` rules.
	import '$lib/components/ui/text-field.css';
	import {
		isPickAny,
		OTHER_OPTION_ID,
		recommendedIds,
		type DecisionDetail,
	} from '$lib/api/attentionQueue';
	import { answers } from '$lib/stores/attentionAnswerState';
	import { draftFor } from '$lib/stores/attentionDock.svelte';
	import { submitAnswer } from '$lib/stores/attentionAnswer.svelte';

	let {
		decision,
		writesDisabled = false,
		onSkip,
	}: {
		decision: DecisionDetail;
		/** True while the surface is offline or showing last-known data. */
		writesDisabled?: boolean;
		/** Present only when another decision waits: move on, keeping this one open. */
		onSkip?: () => void;
	} = $props();

	const draft = $derived(draftFor(decision.id));
	let selected = $state<string | null>(null);
	/** Pick any: the explicit ticks, or `null` while untouched (showing the recommendation). */
	let picked = $state<string[] | null>(null);
	let other = $state('');
	let hydratedFor = $state<string | null>(null);
	let announcement = $state('');
	let otherEl = $state<HTMLTextAreaElement | undefined>();
	let actionsEl = $state<HTMLElement | undefined>();

	$effect(() => {
		if (hydratedFor === decision.id) return;
		hydratedFor = decision.id;
		selected = draft.selected;
		picked = draft.picked;
		other = draft.other;
	});

	/**
	 * Grows the Other field with its content up to the CSS cap, where it
	 * starts scrolling instead. Reruns on every change to `other` — typing
	 * and draft hydration alike — so a restored multi-line draft renders at
	 * its full height immediately, not only after the next keystroke.
	 */
	function resizeOther() {
		if (!otherEl) return;
		otherEl.style.height = 'auto';
		otherEl.style.height = `${otherEl.scrollHeight + otherEl.offsetHeight - otherEl.clientHeight}px`;
		if (document.activeElement === otherEl) revealOther();
	}

	/**
	 * The sticky actions row paints over whatever sits at the bottom of the
	 * scroll region, and the browser only scrolls a focused field clear of its
	 * own viewport edge, not clear of that row. Scroll the field above the row
	 * (the row also clears the keyboard's accessory bar, see `.decision-actions`).
	 */
	function revealOther() {
		if (!otherEl || !actionsEl) return;
		otherEl.style.scrollMarginBottom = `${actionsEl.offsetHeight + 8}px`;
		otherEl.scrollIntoView({ block: 'nearest' });
	}

	/** The keyboard resizes the viewport after focus lands, so reveal once that layout has settled. */
	function revealOtherAfterKeyboard() {
		requestAnimationFrame(() => requestAnimationFrame(revealOther));
	}

	$effect(() => {
		other;
		resizeOther();
	});

	/**
	 * Persist synchronously in the handler, never in a deferred effect: a route
	 * switch in the same task as the keystroke destroys this component before an
	 * effect could flush, which silently loses the draft.
	 */
	function persist() {
		draft.selected = selected;
		draft.picked = picked;
		draft.other = other;
	}

	const headingId = $derived(`decision-${decision.id}`);
	const recommended = $derived(
		decision.options.find((option) => option.id === decision.recommendation) ?? null,
	);
	/** The last send of this decision, when it was rolled back. */
	const failure = $derived.by(() => {
		const answer = $answers[decision.id];
		return answer?.state === 'failed' ? answer : null;
	});
	const locked = $derived(writesDisabled || failure?.kind === 'unauthorized');
	const pickAny = $derived(isPickAny(decision));
	const recommendedSet = $derived(recommendedIds(decision));
	const rationaleOptionId = $derived(
		decision.options.find((option) => recommendedSet.includes(option.id))?.id ?? null,
	);
	const ticked = $derived(picked ?? recommendedSet);
	const asRecommended = $derived(
		ticked.length === recommendedSet.length && ticked.every((id) => recommendedSet.includes(id)),
	);
	/** Pick any answers even with nothing ticked: none is a valid answer. */
	const answerReady = $derived(
		pickAny ? true : selected === OTHER_OPTION_ID ? !!other.trim() : !!selected,
	);
	const canAnswer = $derived(!locked && answerReady);
	const pickAnyReceipt = $derived.by(() => {
		if (ticked.length === 0) {
			return other.trim()
				? 'None chosen, with a note.'
				: 'None chosen. Answering tells the agent you want none of these.';
		}
		const count = `${ticked.length} of ${decision.options.length} chosen`;
		return asRecommended ? `${count} · as recommended` : count;
	});

	/**
	 * Choosing a listed option retires an unsent Other draft, in the model and on
	 * screen, so the two can never disagree and the request can never carry a
	 * combination the server must refuse.
	 */
	function chooseOption(id: string) {
		selected = id;
		other = '';
		announcement = '';
		persist();
	}

	/** Ticking never touches the note, and typing a note never unticks anything. */
	function togglePick(id: string, on: boolean) {
		const next = new Set(ticked);
		if (on) next.add(id);
		else next.delete(id);
		picked = decision.options.map((option) => option.id).filter((optionId) => next.has(optionId));
		announcement = '';
		persist();
	}

	function writeNote(event: Event) {
		other = (event.currentTarget as HTMLTextAreaElement).value;
		announcement = '';
		persist();
	}

	function chooseOther(event?: Event) {
		selected = OTHER_OPTION_ID;
		announcement = '';
		const input = event?.currentTarget as HTMLTextAreaElement | undefined;
		if (input && typeof input.value === 'string') other = input.value;
		persist();
	}

	function submit() {
		if (!canAnswer || (!pickAny && !selected)) return;
		persist();
		announcement = '';
		void submitAnswer(decision);
	}

</script>

<section class="decision-chip" aria-labelledby={headingId}>
	<header>
		<div>
			<h3 id={headingId} tabindex="-1" data-drawer-autofocus>{decision.title}</h3>
			{#if !decision.source.parent_reviewed}
				<p class="source-note">Raised directly — no parent review.</p>
			{/if}
		</div>
	</header>
	<div class="decision-body">
		<div class="decision-top">
			<div class="decision-context">
				<p>{decision.context}</p>
			</div>
			<dl>
				<div><dt>Tradeoff</dt><dd>{decision.tradeoff}</dd></div>
				{#if !rationaleOptionId}
					<div><dt>Why</dt><dd>{decision.rationale}</dd></div>
				{/if}
			</dl>
		</div>
		<div class="options" aria-label="Decision options">
			{#if pickAny}
			<fieldset class="pick-any">
				<legend>Choose any that apply</legend>
				{#each decision.options as option (option.id)}
					{@const isTicked = ticked.includes(option.id)}
					{@const isRecommended = recommendedSet.includes(option.id)}
					{@const carriesRationale = option.id === rationaleOptionId}
					<label class="option-row" class:selected={isTicked}>
						<input
							type="checkbox"
							class="tick"
							checked={isTicked}
							disabled={locked}
							aria-label={isRecommended ? `${option.label}, recommended` : option.label}
							aria-describedby={`${headingId}-${option.id}-detail${carriesRationale ? ` ${headingId}-${option.id}-rationale` : ''}`}
							onchange={(event) => togglePick(option.id, event.currentTarget.checked)}
						/>
						<span class="option-text">
							<span class="option-label-row">
								<b>{option.label}</b>
								{#if isRecommended}
									<span class="recommended">Recommended</span>
								{/if}
							</span>
							<small id={`${headingId}-${option.id}-detail`}>{option.detail}</small>
							{#if carriesRationale}
								<p class="option-rationale" id={`${headingId}-${option.id}-rationale`}><span>Why</span>{decision.rationale}</p>
							{/if}
						</span>
					</label>
				{/each}
				{#if decision.other_enabled}
					<label class="other-option">
						<span class="sr-only">Note</span>
						<textarea
							bind:this={otherEl}
							bind:value={other}
							class="text-field"
							placeholder="Optional: anything else the agent should know"
							disabled={locked}
							rows="1"
							oninput={writeNote}
							onfocus={revealOtherAfterKeyboard}
							onblur={persist}
						></textarea>
					</label>
				{/if}
			</fieldset>
			{:else}
			{#each decision.options as option (option.id)}
				<button
					class="option-button press-tint"
					class:selected={selected === option.id}
					aria-pressed={selected === option.id}
					aria-describedby={`${headingId}-${option.id}-detail${option.id === rationaleOptionId ? ` ${headingId}-${option.id}-rationale` : ''}`}
					disabled={locked}
					onclick={() => chooseOption(option.id)}
				>
					<strong aria-hidden="true">{option.id}</strong>
					<span>
						<span class="option-label-row">
							<b>{option.label}</b>
							{#if recommended && option.id === recommended.id}
								<span class="recommended">Recommended</span>
							{/if}
						</span>
						<small id={`${headingId}-${option.id}-detail`}>{option.detail}</small>
						{#if option.id === rationaleOptionId}
							<p class="option-rationale" id={`${headingId}-${option.id}-rationale`}><span>Why</span>{decision.rationale}</p>
						{/if}
					</span>
				</button>
			{/each}
			{#if decision.other_enabled}
				<label class="other-option">
					<span class="sr-only">Other</span>
					<!-- Choosing Other is a deliberate act: typing into the field, or
					     pointing at it. Merely receiving focus must not choose it —
					     keyboard traversal passes through this input on every pass,
					     and discarding the chosen option there silently unpresses
					     the option and disables Answer with nothing announced. -->
					<textarea
						bind:this={otherEl}
						bind:value={other}
						class="text-field"
						class:selected={selected === OTHER_OPTION_ID}
						placeholder="Type your own response"
						disabled={locked}
						rows="1"
						oninput={chooseOther}
						onpointerdown={chooseOther}
						onfocus={revealOtherAfterKeyboard}
						onblur={persist}
					></textarea>
				</label>
			{/if}
			{/if}
			<div class="decision-actions" bind:this={actionsEl}>
				<p class="receipt" aria-live="polite">
					{announcement || failure?.message || (pickAny ? pickAnyReceipt : answerReady ? 'Ready to send' : 'Choose an option')}
				</p>
				<div class="decision-buttons">
				{#if onSkip}
					<Button variant="secondary" size="sm" ariaLabel="Skip to next decision" onclick={onSkip}>Skip</Button>
				{/if}
				<Button variant="primary" size="sm" disabled={!canAnswer} onclick={submit}>
					{failure?.kind === 'error' ? 'Retry' : 'Answer'}
				</Button>
				</div>
			</div>
			{#if writesDisabled && failure?.kind !== 'unauthorized'}
				<p class="hint">Answering is paused until this reconnects.</p>
			{/if}
		</div>
	</div>
</section>

<style>
	.decision-chip { background: transparent; overflow: visible; }
	/* The decision drawer is opaque: its sticky heading and sticky actions footer must be
	   the same opaque sheet material, otherwise scrolled options visibly pass behind them. */
	header { position: sticky; top: 0; z-index: 2; display: flex; gap: var(--space-3); padding: 0 0 var(--space-3); border-bottom: 1px solid var(--border-subtle); background: var(--bg-secondary); }
	/* Context belongs to the drawer chrome so the title remains the chip's only header fact. */
	h3 { margin: 0; color: var(--text-primary); font-size: var(--text-title3); line-height: var(--lh-tight); }
	.source-note { margin: var(--space-1) 0 0; color: var(--text-tertiary); font-size: var(--text-caption); }
	.decision-body { display: flex; flex-direction: column; gap: var(--space-4); padding: var(--space-4) 0 0; }
	.decision-top { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(220px, .85fr); gap: var(--space-4); }
	.decision-context > p { margin: 0; color: var(--text-primary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	dl { display: grid; gap: var(--space-2); margin: 0; }
	dl div { padding: var(--space-2) var(--space-3); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-secondary); }
	dt { margin-bottom: var(--space-1); color: var(--text-tertiary); font-size: var(--text-footnote); font-weight: var(--fw-semibold); }
	dd { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); line-height: var(--lh-normal); }
	.options { display: flex; flex-direction: column; gap: var(--space-2); }
	.option-button { display: flex; align-items: flex-start; gap: var(--space-2); padding: var(--space-2) var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-primary); color: var(--text-primary); text-align: left; cursor: pointer; }
	.option-button.selected { border-color: var(--accent); background: var(--accent-wash); }
	.option-button:disabled { opacity: 0.5; cursor: not-allowed; }
	.option-button > strong { display: grid; place-items: center; width: 20px; height: 20px; flex: none; border-radius: var(--radius-xs); background: var(--bg-tertiary); color: var(--text-secondary); font-size: var(--text-footnote); }
	.option-button > span { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: var(--space-1); }
	.option-label-row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
	.option-button b { font-size: var(--text-subhead); }
	.option-button small { color: var(--text-tertiary); font-size: var(--text-footnote); line-height: var(--lh-snug); }
	.option-rationale { display: flex; gap: var(--space-1); margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); line-height: var(--lh-snug); }
	.option-rationale span { flex: none; color: var(--text-tertiary); font-weight: var(--fw-semibold); }
	/* Recommended is coloured text, never a filled pill (DESIGN-SYSTEM §0.2). */
	.recommended { color: var(--accent-text); font-size: var(--text-caption); font-weight: var(--fw-semibold); }
	/* The Other and Note fields carry no row of their own: the field spans the option width and is named by its hidden label. */
	.other-option { display: block; }
	.sr-only { position: absolute; inline-size: 0; block-size: 0; margin: 0; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
	/* Other, once chosen, takes the selected-option mark on the field itself; the pick-any Note is never a choice. */
	.text-field.selected { border-color: var(--accent); background: var(--accent-wash); }
	.decision-buttons { display: flex; flex: none; align-items: center; gap: var(--space-2); }
	.decision-actions { position: sticky; bottom: 0; z-index: 2; display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); margin: var(--space-2) 0 0; padding: var(--space-3) 0 max(var(--space-3), env(safe-area-inset-bottom, 0px)); border-top: 1px solid var(--border-subtle); background: var(--bg-secondary); color: var(--text-tertiary); font-size: var(--text-footnote); }
	.receipt { flex: 1; min-width: 0; margin: 0; }
	.hint { margin: 0; color: var(--text-tertiary); font-size: var(--text-caption); }
	fieldset.pick-any { display: flex; flex-direction: column; gap: var(--space-2); min-width: 0; margin: 0; padding: 0; border: 0; }
	fieldset.pick-any legend { padding: 0; margin-bottom: var(--space-2); color: var(--text-tertiary); font-size: var(--text-footnote); font-weight: var(--fw-semibold); }
	.option-row { display: flex; align-items: flex-start; gap: var(--space-2); padding: var(--space-2) var(--space-3); border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-primary); color: var(--text-primary); cursor: pointer; }
	.option-row.selected { border-color: var(--accent); background: var(--accent-wash); }
	.option-row:has(.tick:disabled) { opacity: 0.5; cursor: not-allowed; }
	/* A native checkbox, drawn in CSS: the whole row is its label, so the row is the tap target. */
	.tick { appearance: none; -webkit-appearance: none; display: grid; place-items: center; flex: none; width: 18px; height: 18px; margin: 1px; border: 1.5px solid var(--text-tertiary); border-radius: 5px; background: transparent; cursor: inherit; }
	.tick:checked { border-color: var(--accent); background: var(--accent); }
	.tick:checked::after { content: ''; width: 4px; height: 8px; margin-top: -2px; border: solid #fff; border-width: 0 2px 2px 0; transform: rotate(45deg); }
	.option-text { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: var(--space-1); }
	.option-row b { font-size: var(--text-subhead); }
	.option-row small { color: var(--text-tertiary); font-size: var(--text-footnote); line-height: var(--lh-snug); }
	.option-row:has(.tick:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* The Other / Note field draws its own focus ring, so its row adds none. */
	.option-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* The drawer chrome owns the single context line beside Done at every width. */
	@media (max-width: 720px) {
		.decision-top { grid-template-columns: 1fr; gap: var(--space-3); }
		.option-button { min-height: 44px; }
		.option-row { min-height: 44px; box-sizing: border-box; }
		/* The keyboard's floating accessory bar sits over the bottom of the page: lift the actions above it. */
		.decision-actions { padding-bottom: calc(max(var(--space-3), env(safe-area-inset-bottom, 0px)) + var(--kb-toolbar-inset, 0px)); }
	}
</style>
