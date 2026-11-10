<script lang="ts">
	import AttachmentRow from './AttachmentRow.svelte';
	import type { MessageCard } from '$lib/api/messageCards';
	import { CARD_STATE_LABELS } from '$lib/api/messageCards';
	import type { EmailAttachment } from '$lib/emailDraft';

	// The files the send will attach, as the draft record pins them. Each chip is the transcript's file chip, so it opens
	// the file the way the canvas opens that type today. One pill holds the file's icon, name and size (all of it opens the file).
	// A long name is cut in the middle to the room the size leaves, and the full name stays in the chip's title and accessible name.
	let { attachments, threadId }: {
		attachments: EmailAttachment[];
		threadId: string | null;
	} = $props();

	const card = (item: EmailAttachment): MessageCard => ({
		kind: 'artifact', artifact_id: null, title: item.name, type: item.type === 'image' ? 'image' : 'document',
		icon: item.type === 'image' ? 'image' : 'file-text', name: item.name, path: item.path, available: !item.unavailable, opens: !item.unavailable,
	});
	function size(bytes: number): string {
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
		return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
	}
</script>

{#if attachments.length}
	<ul class="email-attachments" aria-label="Attachments">
		{#each attachments as item (item.name)}
			<li class="attachment">
				<AttachmentRow card={card(item)} {threadId} variant="chip" middleEllipsis chipAside={item.unavailable ? CARD_STATE_LABELS.unavailable : size(item.size)} />
			</li>
		{/each}
	</ul>
{/if}

<style>
	.email-attachments { list-style: none; margin: 0; padding: var(--space-2) calc(var(--space-1) / 2) var(--space-1); display: flex; flex-wrap: wrap; gap: var(--space-2); font-size: var(--text-footnote); line-height: var(--lh-normal); }
	/* One pill holds the chip (icon, name, size): the pill carries the fill, the hover and press tint and the focus ring.
	   Desktop caps it at 320 px; on a narrow screen it takes the line. */
	.attachment {
		display: inline-flex; align-items: center; min-width: 0; max-width: min(100%, calc(var(--space-7) * 10));
		min-height: var(--space-7); padding: 0 var(--space-3); border-radius: var(--radius-pill); background: var(--bg-tertiary);
		transition: background var(--dur-tight) var(--ease-snap);
	}
	.attachment:has(:global(.tile.chip.thumbed)) { padding-left: var(--space-1); }
	@media (hover: hover) { .attachment:has(:global(button.tile.chip:hover)) { background: var(--bg-hover); } }
	.attachment:has(:global(button.tile.chip:active)) { background: var(--press-bg); }
	.attachment:has(:global(button.tile.chip:focus-visible)) { outline: 2px solid var(--accent); outline-offset: 0; }
	@media (prefers-reduced-motion: reduce) { .attachment { transition: none; } }
	/* The chip inside is the pill's open target: no fill, padding or ring of its own (the pill draws them) and it takes the pill's height. */
	.attachment :global(button.tile.chip.press-tint), .attachment :global(.tile.chip) {
		flex: 0 1 auto; min-width: 0; max-width: none; min-height: 0; align-self: stretch; padding: 0; scale: none;
		background: transparent !important; border-radius: var(--radius-pill);
	}
	.attachment :global(button.tile.chip:focus-visible) { outline: none; }
	@media (pointer: coarse) {
		.attachment { min-height: calc(var(--space-6) + var(--space-3)); }
	}
	@media (max-width: 48rem) {
		.attachment { max-width: 100%; }
	}
</style>
