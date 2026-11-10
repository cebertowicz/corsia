<script lang="ts">
	import Popover from '$lib/components/ui/Popover.svelte';
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import AgentOptions from '$lib/components/reviews/AgentOptions.svelte';
	import { agentOptionIndex } from '$lib/components/reviews/agentOptionKeys';
	import { changeOwnership, OwnershipChangeError, type DocOwnership } from '$lib/api/docOwnership';

	let { artifactId, ownership, onChange, expanded = false }: { artifactId: string; ownership: DocOwnership; onChange: (value: DocOwnership) => void; expanded?: boolean } = $props();
	let anchor = $state<HTMLButtonElement>();
	let paneWidth = $state(360);
	function measurePane() {
		const header = anchor?.closest<HTMLElement>('.document-header');
		if (!header) return;
		const style = getComputedStyle(header);
		paneWidth = header.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
	}
	let open = $state(false);
	let saving = $state(false);
	let error = $state('');
	let errorFor = $state<'owner' | 'supervisor' | null>(null);
	let pending = $state<{ kind: 'owner' | 'supervisor'; id: string; name: string } | null>(null);
	let picker = $state<'owner' | 'supervisor' | null>(null);
	let search = $state('');
	let typing = $state(false);
	let active = $state(0);
	const ownerLabel = $derived(pending?.kind === 'owner' ? pending.name : ownership.owner.kind === 'person' ? 'You' : ownership.owner.kind === 'none' ? 'none' : ownership.owner.name ?? 'none');
	const supervisorLabel = $derived(pending?.kind === 'supervisor' ? pending.name : ownership.can_act ? ownership.supervisor?.name ?? 'none' : 'No one gets comments on this document.');
	const sameAgent = $derived(ownership.can_act && (pending?.kind === 'owner' ? pending.id : ownership.owner.kind === 'agent' ? ownership.owner.id : null) === (pending?.kind === 'supervisor' ? pending.id : ownership.supervisor?.id) && !!(pending?.kind === 'supervisor' ? pending.id : ownership.supervisor?.id));
	const agents = $derived(ownership.choices.map((choice) => ({ id: choice.id, name: choice.name, responsibility: choice.status === 'paused' ? 'Paused' : choice.status === 'usage_held' ? 'Usage held' : '' })));
	const matches = $derived((picker === 'owner' && !ownership.multi_person ? [{ id: 'user', name: 'You', responsibility: '' }, ...agents] : agents).filter((choice) => choice.name.toLowerCase().includes(search.trim().toLowerCase())));

	function show(which: 'owner' | 'supervisor', input: HTMLInputElement) {
		picker = which; search = ''; typing = false;
		const selected = which === 'owner' ? (ownership.owner.kind === 'person' ? 'user' : ownership.owner.id) : ownership.supervisor?.id;
		const options = which === 'owner' && !ownership.multi_person ? [{ id: 'user' }, ...agents] : agents;
		active = Math.max(0, options.findIndex((choice) => choice.id === selected));
		input.select();
	}
	function keys(event: KeyboardEvent) {
		if (event.key === 'Escape') { picker = null; return; }
		if (event.key === 'Enter' && matches.length) { event.preventDefault(); void choose(matches[Math.min(active, matches.length - 1)]); return; }
		const index = agentOptionIndex(event.key, active, matches.length);
		if (index !== undefined && index !== null) { event.preventDefault(); active = index; }
	}
	async function choose(choice: { id: string; name: string }) {
		const which = picker;
		if (!which) return;
		// The selected input becomes disabled while saving. Keep focus inside the
		// surface so Escape closes it instead of navigating away from the document.
		const dialog = document.activeElement?.closest<HTMLElement>('[role="dialog"]');
		if (dialog) { dialog.tabIndex = -1; dialog.focus(); }
		picker = null;
		pending = { kind: which, ...choice };
		saving = true;
		error = '';
		errorFor = null;
		try {
			const owner = which === 'owner' ? choice.id : ownership.owner.kind === 'person' ? 'user' : ownership.owner.id ?? 'user';
			const supervisor = which === 'supervisor' ? choice.id : null;
			onChange(await changeOwnership(artifactId, owner, supervisor));
		} catch (cause) {
			errorFor = which;
			error = cause instanceof OwnershipChangeError && cause.status === 409 && which === 'owner' && choice.id === 'user'
				? 'Taking ownership isn’t available when more than one person uses this CORSIA. Choose an agent instead.'
				: !navigator.onLine ? `Couldn’t change the ${which} because you’re offline. Reconnect and try again.` : `Couldn’t change the ${which}. Try again.`;
		}
		finally { pending = null; saving = false; }
	}
</script>

<svelte:window onresize={measurePane} />
<button bind:this={anchor} type="button" class="identity" class:expanded aria-haspopup="dialog" aria-expanded={open} title={`Owner: ${ownerLabel} · Supervisor: ${supervisorLabel}`} onclick={() => { open = !open; if (open) measurePane(); }}>
	{#if sameAgent}<span class="identity-part">Owner and supervisor: <span class="identity-name" class:pending={!!pending}>{ownerLabel}</span></span>{:else}<span class="identity-part">Owner: <span class="identity-name" class:pending={pending?.kind === 'owner'}>{ownerLabel}</span></span><span aria-hidden="true">&nbsp;·&nbsp;</span>{#if ownership.can_act}<span class="identity-part">Supervisor: <span class="identity-name" class:pending={pending?.kind === 'supervisor'}>{supervisorLabel}</span></span>{:else}<span class="identity-part">{supervisorLabel}</span>{/if}{/if}
	{#if saving}<span class="pending">Saving…</span>{:else if error && !open}<span class="failure">Change failed</span>{/if}<span aria-hidden="true">&nbsp;⌄</span>
</button>
{#snippet fields()}
	<div class="owner-picker">
		<label for="owner-choice">Owner</label>
		<input class:pending={pending?.kind === 'owner'} id="owner-choice" aria-describedby={errorFor === 'owner' ? 'owner-error' : undefined} role="combobox" aria-autocomplete="list" aria-expanded={picker === 'owner'} aria-controls="owner-options" aria-activedescendant={picker === 'owner' && matches.length ? `owner-options-option-${matches[Math.min(active, matches.length - 1)].id}` : undefined} disabled={saving} value={picker === 'owner' && typing ? search : ownerLabel} onfocus={(event) => show('owner', event.currentTarget)} oninput={(event) => { typing = true; search = event.currentTarget.value; active = 0; }} onkeydown={keys} onblur={() => picker = null} />
		{#if picker === 'owner'}<AgentOptions id="owner-options" label="Owners" {matches} active={Math.min(active, matches.length - 1)} {choose} compact />{/if}
		{#if error && errorFor === 'owner'}<p id="owner-error" role="status">{error}</p>{/if}
		{#if saving}<span role="status">Saving…</span>{/if}
		<label for="supervisor-choice">Supervisor</label>
		<input class:pending={pending?.kind === 'supervisor'} id="supervisor-choice" aria-describedby={errorFor === 'supervisor' ? 'supervisor-error' : undefined} role="combobox" aria-autocomplete="list" aria-expanded={picker === 'supervisor'} aria-controls="supervisor-options" aria-activedescendant={picker === 'supervisor' && matches.length ? `supervisor-options-option-${matches[Math.min(active, matches.length - 1)].id}` : undefined} disabled={saving} value={picker === 'supervisor' && typing ? search : pending?.kind === 'supervisor' ? pending.name : ownership.supervisor?.name ?? 'No agents in this scope'} onfocus={(event) => show('supervisor', event.currentTarget)} oninput={(event) => { typing = true; search = event.currentTarget.value; active = 0; }} onkeydown={keys} onblur={() => picker = null} />
		{#if picker === 'supervisor'}<AgentOptions id="supervisor-options" label="Supervisors" {matches} active={Math.min(active, matches.length - 1)} {choose} compact />{/if}
		{#if error && errorFor === 'supervisor'}<p id="supervisor-error" role="status">{error}</p>{/if}
	</div>
{/snippet}
{#if open && paneWidth < 360}
	<AdaptiveDrawer ariaLabel="Owner and supervisor" heading="Owner and supervisor" onClose={() => { open = false; picker = null; }}>
		{@render fields()}
	</AdaptiveDrawer>
{:else}
	<Popover {anchor} {open} bounds={anchor?.closest('.doc') as HTMLElement | null} placement="bottom" width={`min(360px, ${Math.max(0, paneWidth - 24)}px)`} onClose={() => { open = false; picker = null; }}>
		{@render fields()}
	</Popover>
{/if}

<style>
	.identity { display: flex; gap: var(--space-1); text-decoration: underline; text-underline-offset: 2px; min-width: 0; max-width: 100%; white-space: nowrap; text-align: left; border: 0; padding: 0; background: none; color: inherit; font: inherit; cursor: pointer; }
	.identity-part { display: flex; min-width: 0; white-space: pre; }
	.identity-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.identity.expanded { display: block; white-space: normal; }
	.identity.expanded .identity-part { display: inline; white-space: normal; }
	.identity.expanded .identity-name { overflow: visible; overflow-wrap: anywhere; }
	.identity:hover { color: var(--text-primary); }
	.pending { opacity: 0.65; }
	.failure { color: var(--red-text); }
	@media (any-pointer: coarse) { .identity { min-height: 44px; align-items: center; } }
	.identity:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.owner-picker { display: flex; flex-direction: column; gap: var(--space-1); padding: var(--space-3); }
	label { color: var(--text-secondary); font-size: var(--text-footnote); }
	input { box-sizing: border-box; width: 100%; min-width: 0; min-height: var(--touch-target); padding: var(--space-2); color: var(--text-primary); background: var(--bg-primary); border: 1px solid var(--border); border-radius: var(--radius-lg); font: inherit; }
	p { margin: 0; color: var(--red-text); font-size: var(--text-footnote); }
</style>
