<script lang="ts">
	// Typed artifact icon. The backend presentation registry chooses the icon
	// name, so a new artifact type needs a case here — not a change to pane
	// hierarchy control flow.
	let { icon }: { icon: string } = $props();

	const ICON_COLOR: Record<string, string> = {
		'file-text': 'var(--violet-2, var(--violet))',
		'file-code': 'var(--amber)',
		sparkles: 'var(--teal)',
		paperclip: 'var(--green)',
		'git-pull-request': 'var(--sky)'
	};
	const color = $derived(ICON_COLOR[icon] ?? 'var(--text-tertiary)');
</script>

<span class="ic" style="color: {color}" aria-hidden="true">
	{#if icon === 'file-text'}
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
	{:else if icon === 'file-code'}
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m8 6-5 6 5 6M16 6l5 6-5 6" /></svg>
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
	.ic { display: flex; flex-shrink: 0; }
	.ic svg { width: 15px; height: 15px; }
</style>
