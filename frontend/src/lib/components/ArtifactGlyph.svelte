<script lang="ts">
	// Typed artifact icon. The backend presentation registry chooses the icon
	// name, so a new artifact type needs a case here — not a change to pane
	// hierarchy control flow.
	// Every list icon is the tertiary grey: the glyph's shape carries the type, colour is kept for meaning.
	let { icon, tile = 0 }: {
		icon: string;
		/** Side in px of a filled type tile the icon sits centred in; 0 draws the bare icon. */
		tile?: number;
	} = $props();
</script>

<span class="ic" class:filled={tile > 0} style="--tile: {tile}px" aria-hidden="true">
	{#if icon === 'file-text'}
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
	{:else if icon === 'file-code'}
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m8 6-5 6 5 6M16 6l5 6-5 6" /></svg>
	{:else if icon === 'image'}
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="m21 16-5-5-8 9" /></svg>
	{:else if icon === 'sparkles'}
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.7 4.8L18.5 9l-4.8 1.7L12 15.5l-1.7-4.8L5.5 9l4.8-1.7L12 3z" /><path d="M19 14.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2z" /></svg>
	{:else if icon === 'paperclip'}
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.5 12.5 21a4.2 4.2 0 0 1-6-6L15 6.5a2.8 2.8 0 0 1 4 4L10.5 19a1.4 1.4 0 0 1-2-2l7.5-7.5" /></svg>
	{:else if icon === 'git-pull-request'}
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="6" cy="6" r="2.5" /><circle cx="6" cy="18" r="2.5" /><circle cx="18" cy="16" r="2.5" /><path d="M6 8.5v7M18 13.5V11a4 4 0 0 0-4-4H9" /></svg>
	{:else}
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /><path d="M9.7 12.6a1.6 1.6 0 1 1 2.4 1.4c-.7.4-1.1.9-1.1 1.7" /><path d="M11 18.2h.01" /></svg>
	{/if}
</span>

<style>
	.ic { display: flex; flex-shrink: 0; color: var(--text-tertiary); }
	.ic svg { width: 15px; height: 15px; }
	/* The tile keeps the row's geometry; the icon's shape carries the type, in the shared --text-tertiary, with no tint behind it. */
	.ic.filled {
		align-items: center;
		justify-content: center;
		width: var(--tile);
		height: var(--tile);
	}
	.ic.filled svg { width: 55%; height: 55%; }
</style>
