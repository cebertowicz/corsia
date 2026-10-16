<script lang="ts">
	import { tick } from 'svelte';
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import { Button } from '$lib/components/ui';
	import { agentColor } from '$lib/docPresence';
	import type { DocumentReviewState } from '$lib/docReviewState.svelte';
	let { model, id, onSent }: { model: DocumentReviewState; id: string; onSent: () => void } = $props();
	let search = $state('');
	let active = $state(0);
	let focused = $state(false);
	let pressing = false;
	let searchInput = $state<HTMLInputElement>();
	const matches = $derived(model.choices().filter((reviewer) => reviewer.name.toLowerCase().includes(search.trim().toLowerCase())));
	const open = $derived(model.loadState === 'ready' && !model.rosterError && (focused || !!search) && matches.length > 0);
	const activeReviewer = $derived(open ? matches[Math.min(active, matches.length - 1)] : undefined);
	const nobody = $derived(model.loadState === 'ready' && !model.rosterError && !model.choices().length && !model.selected.length);
	const disabledReason = $derived(model.selected.length ? '' : 'Choose a reviewer to send');
	function add(reviewer: (typeof matches)[number]) {
		model.select(reviewer);
		search = ''; active = 0;
		void tick().then(() => searchInput?.focus());
	}
	function remove(reviewerId: string) {
		model.deselect(reviewerId);
		void tick().then(() => searchInput?.focus());
	}
	function keys(event: KeyboardEvent) {
		if (event.key === 'Backspace' && !search && model.selected.length) { event.preventDefault(); model.deselect(model.selected[model.selected.length - 1].id); return; }
		if (!['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter'].includes(event.key)) return;
		if (event.key === 'Enter') { if (activeReviewer) { event.preventDefault(); add(activeReviewer); } return; }
		event.preventDefault();
		if (!matches.length) return;
		if (event.key === 'Home') active = 0;
		else if (event.key === 'End') active = matches.length - 1;
		else if (event.key === 'ArrowDown') active = (active + 1) % matches.length;
		else active = active <= 0 ? matches.length - 1 : active - 1;
	}
	// A press that starts on another control (Send) blurs the field first; closing the list then would move that control out from under the press.
	function blurred() {
		if (!pressing) { focused = false; return; }
		window.addEventListener('pointerup', () => setTimeout(() => { focused = document.activeElement === searchInput; }, 0), { once: true });
	}
	async function submit(event: SubmitEvent) { event.preventDefault(); if (await model.submit()) onSent(); }
</script>

<svelte:window onpointerup={() => pressing = false} />
<form onsubmit={submit} onpointerdown={() => pressing = true}>
	<label class="field-label" for={`${id}-search`}>Add reviewers</label>
	<div class="chip-field" class:focused>
		{#each model.selected as reviewer (reviewer.id)}
			<span class="chip" class:failed={!!model.failures[reviewer.id]}><Avatar name={reviewer.name} color={agentColor(reviewer.id)} hidden /><span class="chip-name">{reviewer.name}</span>
				<button type="button" class="chip-remove" aria-label={`Remove ${reviewer.name}`} onclick={() => remove(reviewer.id)}><svg aria-hidden="true" viewBox="0 0 16 16"><path d="m4 4 8 8M12 4 4 12" /></svg></button></span>
		{/each}
		<input bind:this={searchInput} id={`${id}-search`} data-review-autofocus role="combobox" aria-autocomplete="list" aria-expanded={open}
			aria-controls={`${id}-options`} aria-activedescendant={activeReviewer ? `${id}-option-${activeReviewer.id}` : undefined}
			placeholder={model.selected.length ? '' : 'Find an agent'} value={search} autocomplete="off"
			oninput={(event) => { search = event.currentTarget.value; active = 0; }} onkeydown={keys}
			onfocus={() => focused = true} onblur={blurred} />
	</div>
	{#if open}
		<!-- tabindex -1: a scrolling list is a Tab stop in Chromium, and it unmounts when the field loses focus, dropping focus to the page. -->
		<div id={`${id}-options`} role="listbox" tabindex="-1" aria-label="Agents that can review" class="options">
			{#each matches as reviewer, index (reviewer.id)}
				<!-- svelte-ignore a11y_click_events_have_key_events (The combobox input owns the keyboard path.) -->
				<div id={`${id}-option-${reviewer.id}`} role="option" tabindex="-1" aria-selected={false} class="option" class:active={index === Math.min(active, matches.length - 1)}
					onmousedown={(event) => event.preventDefault()} onclick={() => add(reviewer)}>
					<Avatar name={reviewer.name} color={agentColor(reviewer.id)} hidden size="md" />
					<span class="option-text">
						<span class="option-name">{reviewer.name}</span>
						{#if reviewer.responsibility}<span class="option-detail" title={reviewer.responsibility}>{reviewer.responsibility}</span>{/if}
					</span>
				</div>
			{/each}
		</div>
	{:else}
		<div id={`${id}-options`} role="listbox" aria-label="Agents that can review" hidden></div>
	{/if}
	{#if model.loadState === 'loading'}<p class="hint" role="status">Loading reviewers…</p>
	{:else if model.loadState === 'error'}<p class="hint" role="status">Couldn't load review requests. <Button size="sm" variant="ghost" onclick={() => model.load()}>Retry</Button></p>
	{:else if model.rosterError}<p class="hint" role="status">Couldn't load reviewers. <Button size="sm" variant="ghost" onclick={() => model.load()}>Retry</Button></p>
	{:else if nobody}<p class="hint" role="status">No agents in this project can review right now.</p>
	{:else if search.trim() && !matches.length}<p class="hint" role="status">No matching agents.</p>
	{/if}
	{#if model.selected.length}
		<label class="field-label" for={`${id}-note`}>Note <span class="optional">(optional)</span></label>
		<textarea id={`${id}-note`} maxlength="500" rows="2" placeholder="What should they look at?" bind:value={model.note}></textarea>
		{#if model.note.length >= 400}<span class="hint counter">{model.note.length} / 500</span>{/if}
	{/if}
	{#if model.refusal}
		<div class="error" role="alert">
			<p>{model.refusal}</p>
			{#if Object.keys(model.failures).length}
				<ul>{#each model.selected as reviewer (reviewer.id)}{#if model.failures[reviewer.id]}<li><strong>{reviewer.name}</strong>: {model.failures[reviewer.id]}</li>{/if}{/each}</ul>
			{/if}
		</div>
	{/if}
	<footer>
		{#if disabledReason}<span class="hint" id={`${id}-why`}>{disabledReason}</span>{/if}
		<button type="submit" class="send" aria-describedby={disabledReason ? `${id}-why` : undefined} disabled={!model.selected.length || model.sending || model.note.length > 500} aria-busy={model.sending}>
			{model.sending ? 'Sending…' : model.refusal ? 'Retry' : 'Send'}
		</button>
	</footer>
</form>

<style>
	form { display: flex; flex-direction: column; gap: 6px; min-width: 0; min-height: 0; font-family: var(--font-sans); font-size: var(--text-subhead); color: var(--text-primary); }
	.field-label { font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	.optional { font-weight: normal; color: var(--text-tertiary); }
	.chip-field { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; min-height: 32px; padding: 3px 6px; box-sizing: border-box; background: var(--bg-primary); border: 1px solid var(--border); border-radius: var(--radius-sm); }
	.chip-field.focused { border-color: var(--accent); }
	.chip-field input { flex: 1 1 80px; min-width: 60px; min-height: 22px; padding: 0; border: 0; outline: 0; background: none; color: var(--text-primary); font: inherit; }
	.chip { display: inline-flex; align-items: center; gap: 4px; max-width: 100%; padding: 1px 2px 1px 3px; background: var(--bg-tertiary); border-radius: var(--radius-pill); font-size: var(--text-footnote); }
	.chip-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.chip-remove { display: inline-grid; place-items: center; width: 18px; height: 18px; padding: 0; border: 0; border-radius: var(--radius-circle); background: none; color: var(--text-secondary); cursor: pointer; }
	.chip-remove:hover { background: var(--bg-hover); color: var(--text-primary); }
	.chip-remove svg { width: 10px; height: 10px; fill: none; stroke: currentColor; stroke-width: 1.75; stroke-linecap: round; }
	.chip-remove:focus-visible { outline: 2px solid var(--accent); }
	.options { max-height: 176px; overflow: auto; overscroll-behavior: contain; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-elevated); }
	.option { display: flex; align-items: center; gap: 8px; min-height: 36px; padding: 4px 8px; cursor: pointer; }
	.option.active, .option:hover { background: var(--bg-hover); }
	.option-text { display: flex; flex-direction: column; min-width: 0; }
	.option-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.option-detail { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-secondary); font-size: var(--text-footnote); }
	textarea { width: 100%; box-sizing: border-box; resize: none; padding: 6px 8px; font: inherit; color: var(--text-primary); background: var(--bg-primary); border: 1px solid var(--border); border-radius: var(--radius-sm); }
	textarea:focus-visible { outline: 0; border-color: var(--accent); }
	.hint { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); overflow-wrap: anywhere; }
	.error { color: var(--red); font-size: var(--text-footnote); overflow-wrap: anywhere; }
	.error p { margin: 0; }
	.error ul { margin: 2px 0 0; padding-left: 16px; }
	.chip.failed { box-shadow: inset 0 0 0 1px var(--red); }
	.send { position: relative; height: 28px; padding: 0 12px; border: 1px solid var(--accent); border-radius: var(--radius-sm); background: var(--accent); color: var(--on-accent); font: inherit; font-size: var(--text-footnote); font-weight: var(--fw-semibold); cursor: pointer; transition: filter var(--dur-tight) var(--ease-snap); }
	.send:hover:not(:disabled) { filter: brightness(1.08); }
	.send:disabled { background: var(--accent-bg); border-color: var(--border); color: var(--text-tertiary); cursor: not-allowed; }
	.send[aria-busy="true"] { cursor: wait; }
	@media (max-width: 768px) { .send::after { content: ''; position: absolute; inset: -8px; } }
	footer { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 2px; }
	@media (max-width: 768px) {
		.chip-field { min-height: 44px; }
		.chip-remove { width: 28px; height: 28px; }
		.option { min-height: 44px; }
	}
</style>
