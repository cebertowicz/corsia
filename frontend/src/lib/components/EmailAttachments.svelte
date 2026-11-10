<script lang="ts">
	import AttachmentRow from './AttachmentRow.svelte';
	import type { MessageCard } from '$lib/api/messageCards';
	import { CARD_STATE_LABELS } from '$lib/api/messageCards';
	import type { EmailAttachment } from '$lib/emailDraft';
	import { prefersReducedMotion } from '$lib/stores/viewport';

	// The files the send will attach, as the draft record pins them. Each chip is the transcript's file chip, so it opens
	// the file the way the canvas opens that type today. One pill holds the file's icon, name and size (all of it opens the file)
	// and the remove mark. A long name is cut in the middle to the room the size and the mark leave, and the full name stays in the
	// chip's title and accessible name.
	let { attachments, threadId, openedFrom = null, onremove }: {
		attachments: EmailAttachment[];
		threadId: string | null;
		/** The draft's own tab (its instance key): a file opened from a chip can lead back to it. */
		openedFrom?: string | null;
		onremove?: (attachment: EmailAttachment) => void;
	} = $props();

	// The row is rebuilt when a file leaves the list, which would make the chip vanish: fade it out first, then hand the removal on.
	const FADE_MS = 160;
	let leaving = $state<Set<string>>(new Set());
	function remove(item: EmailAttachment) {
		if (!onremove || leaving.has(item.name)) return;
		if ($prefersReducedMotion) { onremove(item); return; }
		leaving = new Set([...leaving, item.name]);
		setTimeout(() => onremove(item), FADE_MS);
	}

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
			<li class="attachment" class:leaving={leaving.has(item.name)}>
				<AttachmentRow card={card(item)} {threadId} {openedFrom} variant="chip" middleEllipsis chipAside={item.unavailable ? CARD_STATE_LABELS.unavailable : size(item.size)} />
				{#if onremove}
					<button type="button" class="attachment-remove" aria-label="Remove {item.name}" onclick={() => remove(item)}>
						<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
					</button>
				{/if}
			</li>
		{/each}
	</ul>
{/if}

<style>
	.email-attachments { list-style: none; margin: 0; padding: var(--space-2) 2px var(--space-1); display: flex; flex-wrap: wrap; gap: var(--space-2); font-size: var(--text-footnote); line-height: var(--lh-normal); }
	/* One pill holds the chip (icon, name, size) and the remove mark: the pill carries the fill, the hover and press tint and the
	   focus ring, so nothing floats outside it. Desktop caps it at 320 px; on a narrow screen it takes the line. */
	.attachment {
		display: inline-flex; align-items: center; gap: var(--space-1); min-width: 0; max-width: min(100%, 320px);
		min-height: 32px; padding: 0 var(--space-1) 0 var(--space-3); border-radius: var(--radius-pill); background: var(--bg-tertiary);
		transition: opacity 160ms ease-out, transform 160ms ease-out, background var(--dur-tight) var(--ease-snap);
	}
	.attachment:has(:global(.tile.chip.thumbed)) { padding-left: var(--space-1); }
	@media (hover: hover) { .attachment:has(:global(button.tile.chip:hover)) { background: var(--bg-hover); } }
	.attachment:has(:global(button.tile.chip:active)) { background: var(--press-bg); }
	.attachment:has(:global(button.tile.chip:focus-visible)) { outline: 2px solid var(--accent); outline-offset: 0; }
	.attachment.leaving { opacity: 0; transform: scale(0.96); pointer-events: none; }
	@media (prefers-reduced-motion: reduce) { .attachment { transition: none; } }
	/* The chip inside is the pill's open target: no fill, padding or ring of its own (the pill draws them) and it takes the pill's height. */
	.attachment :global(button.tile.chip.press-tint), .attachment :global(.tile.chip) {
		flex: 0 1 auto; min-width: 0; max-width: none; min-height: 0; align-self: stretch; padding: 0; scale: none;
		background: transparent !important; border-radius: var(--radius-pill);
	}
	.attachment :global(button.tile.chip:focus-visible) { outline: none; }
	.attachment-remove { all: unset; box-sizing: border-box; position: relative; cursor: pointer; flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; border-radius: var(--radius-pill); color: var(--text-tertiary); }
	@media (hover: hover) { .attachment-remove:hover { color: var(--text-primary); background: var(--bg-hover); } }
	.attachment-remove:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
	/* On touch the mark is drawn at 24 px and the press target is 44 px (--touch-target) around it, reaching past the pill and kept
	   clear of the size text by the gap before it. */
	@media (pointer: coarse) {
		.attachment { min-height: 36px; }
		.attachment-remove { margin-left: var(--space-3); }
		.attachment-remove::after { content: ''; position: absolute; top: 50%; left: 50%; width: var(--touch-target); height: var(--touch-target); transform: translate(-50%, -50%); }
	}
	@media (max-width: 768px) {
		.attachment { max-width: 100%; }
	}
</style>
