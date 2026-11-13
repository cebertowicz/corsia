<script lang="ts">
	import { renderMentions, type TaggedAgent } from './mentionMarkup';
	import './comment-link.css';
	import type { MentionOccurrence } from './mentionSelection';
	import { isLongMessageText } from '$lib/messageCollapse';

	let { text, unread = false, preview = false, taggedAgents = [], mentions = [], taggedPerson = null }: { text: string; unread?: boolean; preview?: boolean; taggedAgents?: TaggedAgent[]; mentions?: MentionOccurrence[]; taggedPerson?: string | null } = $props();
	// Chat's one rule for a long message decides; an unread body always shows whole. `unread` is the thread's held
	// state (CommentThread), which stays set while the thread is mounted, so nothing collapses under the person reading it.
	// A resting margin card's preview is not being read: it clamps at its short height until the card is picked.
	const collapsible = $derived(preview || (!unread && isLongMessageText(text)));
	let open = $state(false);
	let clamped = $state(false);
	let reveal: HTMLDivElement;
	let expandedHeight = $state(0);

	// The parameter is the collapsible flag, passed only so the action re-measures when it changes.
	function trackClamp(node: HTMLElement, _collapsible: boolean) {
		const measure = () => {
			if (!reveal) return;
			if (open) expandedHeight = node.scrollHeight;
			else clamped = collapsible && node.scrollHeight > reveal.clientHeight;
		};
		if (typeof ResizeObserver === 'undefined') {
			requestAnimationFrame(measure);
			return { update: measure, destroy: () => {} };
		}
		const observer = new ResizeObserver(measure);
		observer.observe(node);
		// Its clamp changes with the card (a margin card's preview clamps sooner than the card opened), so whether it clips is measured again.
		// The reveal box is the parent; `reveal` is not bound yet while this child mounts.
		if (node.parentElement) observer.observe(node.parentElement);
		requestAnimationFrame(measure);
		return { update: measure, destroy: () => observer.disconnect() };
	}

	function toggle() {
		if (!open) expandedHeight = reveal.scrollHeight;
		open = !open;
	}
</script>

<div bind:this={reveal} class:open class:clamped={clamped && !open} class:whole={!collapsible} class="ct-markdown-reveal" style:--ct-expanded-height="{expandedHeight}px">
	<div class="ct-markdown md-content" use:trackClamp={collapsible}>{@html renderMentions(text, taggedAgents, mentions, taggedPerson)}</div>
</div>
{#if clamped}<button class="ct-more" onclick={(e) => { e.stopPropagation(); toggle(); }}>{open ? 'Show less' : 'Show more'}</button>{/if}

<style>
	.ct-markdown-reveal { flex-shrink: 0; --ct-expanded-height: calc(var(--ct-body-lines, 4) * 1lh); max-height: calc(var(--ct-body-lines, 4) * 1lh); overflow: hidden; transition: max-height var(--dur-standard) var(--ease-entrance); }
	.ct-markdown-reveal.clamped { -webkit-mask-image: linear-gradient(to bottom, black calc(100% - 1lh), transparent); mask-image: linear-gradient(to bottom, black calc(100% - 1lh), transparent); }
	.ct-markdown-reveal.open { max-height: var(--ct-expanded-height); }
	.ct-markdown-reveal.whole { max-height: none; }
	.ct-markdown { overflow-wrap: anywhere; font-size: var(--text-subhead); line-height: var(--lh-snug); color: var(--text-primary); }
	.ct-markdown :global(:is(h1, h2, h3, h4, h5, h6)) { font: inherit; font-weight: var(--fw-semibold); margin: 0; }
	.ct-markdown :global(p:first-child) { margin-top: 0; }
	.ct-markdown :global(p:last-child) { margin-bottom: 0; }
	.ct-markdown :global(.comment-mention) { color: var(--text-primary); font-weight: var(--fw-semibold); }
	.ct-more { align-self: flex-start; min-height: var(--space-6); margin-top: calc(var(--space-1) * -1); padding: 0; border: 0; background: none; color: var(--text-secondary); font: inherit; font-size: var(--text-caption); font-weight: var(--fw-medium); cursor: pointer; }
	.ct-more:hover { text-decoration: underline; }
	@media (pointer: coarse) { .ct-more { min-height: var(--touch-target); } }
	@media (prefers-reduced-motion: reduce) { .ct-markdown-reveal { transition: none; } }
</style>
