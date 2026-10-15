<script lang="ts">
	import { browserAddressLabel } from '$lib/browserAddress';
	import AvatarStack, { type AvatarParticipant } from '$lib/components/ui/AvatarStack.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import IconButton from '$lib/components/ui/IconButton.svelte';

	let { url, domain = null, streaming, ended, status, announcement, people, agentPresent, onnavigate, onhistory, onreconnect }: {
		url: string;
		/** The page's registrable domain as the backend names it, the same one a password step's title shows. */
		domain?: string | null;
		streaming: boolean;
		/** The stream ended or failed, so the person can reconnect. */
		ended: boolean;
		/** Shown only when the person needs it: opening, an error, or an ended stream. */
		status: string;
		/** Read politely by assistive technology and never shown. */
		announcement: string;
		people: AvatarParticipant[];
		agentPresent: boolean;
		onnavigate: (url: string) => void;
		onhistory: (action: 'back' | 'forward' | 'reload') => void;
		onreconnect: () => void;
	} = $props();

	let addressFocused = $state(false);
	let addressText = $state('');
	const shownAddress = $derived(addressFocused ? addressText : browserAddressLabel(url, domain));

	function submitAddress(event: SubmitEvent) {
		event.preventDefault();
		addressFocused = false;
		onnavigate(addressText);
	}

	function focusAddress(event: FocusEvent) {
		addressText = url;
		addressFocused = true;
		// Swap in the full URL now and select it, as a browser does. Left to the next render,
		// the swap would land after a selection made on focus and append the person's text to it.
		const input = event.currentTarget as HTMLInputElement;
		input.value = url;
		input.select();
	}

	function editAddress(event: Event) {
		addressText = (event.currentTarget as HTMLInputElement).value;
	}
</script>

<form class="toolbar" onsubmit={submitAddress}>
	<IconButton ariaLabel="Back" title="Back" size="sm" onclick={() => onhistory('back')} disabled={!streaming}>
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
	</IconButton>
	<IconButton ariaLabel="Forward" title="Forward" size="sm" onclick={() => onhistory('forward')} disabled={!streaming}>
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
	</IconButton>
	<IconButton ariaLabel="Reload" title="Reload" size="sm" onclick={() => onhistory('reload')} disabled={!streaming}>
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" /></svg>
	</IconButton>
	<div class="agent-seam" class:present={agentPresent}><AvatarStack {people} max={3} /></div>
	<!-- Enter submits: the form's only text field. -->
	<div class="address">
		<input aria-label="Browser address" value={shownAddress} placeholder="http:// or https://" onfocus={focusAddress} onblur={() => addressFocused = false} oninput={editAddress} />
	</div>
	{#if ended}<Button size="sm" onclick={onreconnect}>Reconnect</Button>{/if}
	{#if status}<span class="status" title={status}>{status}</span>{/if}
	<span class="sr-only" aria-live="polite">{announcement}</span>
</form>

<style>
	.toolbar { display: flex; flex-shrink: 0; gap: 6px; align-items: center; padding: 6px 8px; }
	.toolbar :global(.ico-btn svg) { width: 16px; height: 16px; }
	.status { flex-shrink: 1; min-width: 0; max-width: 40%; overflow: hidden; color: var(--text-secondary); font-size: var(--text-footnote); white-space: nowrap; text-overflow: ellipsis; }
	.agent-seam { display: inline-flex; flex-shrink: 0; opacity: 0; visibility: hidden; transition: opacity 160ms ease, visibility 0s linear 160ms; }
	.agent-seam.present { opacity: 1; visibility: visible; transition: opacity 160ms ease; }
	/* The address field reuses the app's search field (DocumentsView .search). */
	.address { flex: 1; display: flex; align-items: center; min-width: 0; padding: 5px 10px; border-radius: var(--radius-sm); background: var(--bg-secondary); border: 1px solid var(--border-subtle); }
	.address:focus-within { border-color: var(--accent); }
	/* The address leads with the registrable domain, so ending the clip on the right keeps the domain whole. */
	.address input { flex: 1; min-width: 0; background: none; border: none; outline: none; font: inherit; font-size: var(--text-subhead); color: var(--text-primary); text-overflow: ellipsis; }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
	@media (prefers-reduced-motion: reduce) { .agent-seam, .agent-seam.present { transition: none; } }
	@media (max-width: 768px) { .address input { font-size: var(--text-input, 16px); } }
</style>
