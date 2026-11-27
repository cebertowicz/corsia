<script lang="ts">
	import { keepInView } from '$lib/actions/keepInView';
	import { isPickAny, OTHER_OPTION_ID, type DecisionDetail } from '$lib/api/attentionQueue';

	/** A decision's options: one or any, and the person's own answer. The draft is the owner's; each change reports through `onChange`. */
	let { decision, rationaleOptionId, sending, offline, selected = $bindable(), picked = $bindable(), other = $bindable(), onChange }: {
		decision: DecisionDetail;
		rationaleOptionId: string | null;
		sending: boolean;
		offline: boolean;
		selected: string | null;
		picked: string[];
		other: string;
		onChange: () => void;
	} = $props();
	let fieldFocused = $state(false);

	function togglePicked(id: string) {
		picked = picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id];
		onChange();
	}
</script>

<p class="opts-label" id="decision-{decision.id}-label">{isPickAny(decision) ? 'Choose any' : 'Choose one'}</p>
<fieldset class="choices" aria-labelledby="decision-{decision.id}-label">
	{#each decision.options as option (option.id)}
		{@const recommended = Array.isArray(decision.recommendation) ? decision.recommendation.includes(option.id) : decision.recommendation === option.id}
		{@const rationaleId = option.id === rationaleOptionId && decision.rationale ? `decision-${decision.id}-${option.id}-rationale` : undefined}
		{@const chosen = isPickAny(decision) ? picked.includes(option.id) : selected === option.id}
		<label class="choice" class:sel={chosen}>
			<svg class="mark" viewBox="0 0 12 9" aria-hidden="true"><path d="M1 4.6 4.3 8 11 1" /></svg>
			{#if isPickAny(decision)}
				<input type="checkbox" checked={chosen} aria-describedby={rationaleId} onchange={() => togglePicked(option.id)} disabled={sending || offline} />
			{:else}
				<input type="radio" name="decision-{decision.id}" value={option.id} checked={chosen} aria-describedby={rationaleId} onchange={() => { selected = option.id; onChange(); }} disabled={sending || offline} />
			{/if}
			<span class="ctext"><span class="chead"><b>{option.label}</b></span>{#if recommended}<em class="rec">Recommended</em>{/if}{#if option.detail}<span class="cdetail">{option.detail}</span>{/if}{#if rationaleId}<span class="cwhy" id={rationaleId}><span class="wl">Why</span><span class="wt">{decision.rationale}</span></span>{/if}</span>
		</label>
	{/each}
	{#if decision.other_enabled && !isPickAny(decision)}
		<label class="choice else" class:sel={selected === OTHER_OPTION_ID}>
			<svg class="mark" viewBox="0 0 12 9" aria-hidden="true"><path d="M1 4.6 4.3 8 11 1" /></svg>
			<input type="radio" name="decision-{decision.id}" value={OTHER_OPTION_ID} checked={selected === OTHER_OPTION_ID} onchange={() => { selected = OTHER_OPTION_ID; onChange(); }} disabled={sending || offline} />
			<span class="ctext"><span class="chead"><b>Something else</b></span><span class="cdetail">Select to write your own answer.</span>
				{#if selected === OTHER_OPTION_ID}
					<textarea class="other" rows="3" use:keepInView={{ enabled: fieldFocused }} onfocus={() => { fieldFocused = true; }} onblur={() => { fieldFocused = false; }} bind:value={other} oninput={(event) => { other = event.currentTarget.value; onChange(); }} disabled={sending || offline}
						placeholder="Say what you want instead" aria-label="Your answer"></textarea>
				{/if}
			</span>
		</label>
	{/if}
</fieldset>
{#if decision.other_enabled && isPickAny(decision)}
	<textarea class="other note" rows="3" use:keepInView={{ enabled: fieldFocused }} onfocus={() => { fieldFocused = true; }} onblur={() => { fieldFocused = false; }} bind:value={other} oninput={(event) => { other = event.currentTarget.value; onChange(); }} disabled={sending || offline}
		placeholder="Add a note (optional)" aria-label="Note"></textarea>
{/if}

<style>
	.opts-label { margin: var(--space-3) 0 var(--space-5); color: var(--text-tertiary); font-size: var(--text-footnote); font-weight: var(--fw-semibold); }
	.choices { display: flex; flex-direction: column; gap: var(--space-2); margin: 0 calc(-1 * var(--row-right)) 0 calc(-1 * var(--row-left)); padding: 0; border: 0; min-width: 0; }
	.choice { position: relative; display: flex; align-items: flex-start; min-height: var(--list-row-min-height); padding: var(--space-3) var(--row-right) var(--space-3) var(--row-left); border-radius: var(--radius-sm); background: transparent; font-size: var(--text-body); line-height: var(--lh-normal); cursor: pointer; --head-fs: var(--text-body); }
	:global(.activity.phone) .choice { min-height: var(--touch-target); font-size: var(--text-input); border-radius: 0; --head-fs: var(--text-input); }
	.choice:hover { background: var(--bg-hover); }
	.choice.sel, .choice.sel:hover { background: var(--accent-wash); }
	.choice input { position: absolute; opacity: 0; pointer-events: none; }
	.choice:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
	.ctext { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--space-1); line-height: var(--lh-snug); }
	.chead b { font-weight: var(--fw-semibold); color: var(--text-primary); }
	.rec { font-style: normal; color: var(--accent-text); font-size: var(--text-footnote); line-height: var(--lh-snug); }
	.cdetail { color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-snug); }
	:global(.activity.phone) .cdetail { font-size: var(--text-callout); }
	.cwhy { display: flex; align-items: baseline; gap: var(--space-2); color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-snug); }
	:global(.activity.phone) .cwhy { font-size: var(--text-callout); }
	.cwhy .wl { flex: none; color: var(--text-tertiary); font-weight: var(--fw-semibold); }
	.cwhy .wt { min-width: 0; }
	.mark { position: absolute; left: var(--row-inset); top: calc(var(--space-3) + (var(--head-fs) * 1.4 - var(--check-w) * 9 / 12) / 2); width: var(--check-w); height: auto; fill: none; stroke: var(--accent-text); stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; visibility: hidden; }
	.choice.sel .mark { visibility: visible; }
	.note { margin-top: var(--space-3); }
	.other {
		display: block; width: 100%; box-sizing: border-box; min-height: calc(var(--space-7) + var(--space-8)); margin-top: var(--space-2); padding: var(--space-2) var(--space-3);
		border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--glass-thin);
		box-shadow: var(--glass-rim), var(--shadow-chip); color: var(--text-primary); font: inherit; font-size: var(--text-input); line-height: var(--lh-normal); resize: none;
	}
	.other:focus { outline: none; border-color: var(--accent); box-shadow: var(--glass-rim), var(--shadow-chip), var(--focus-ring); }
</style>
