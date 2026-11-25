import type { Snippet } from 'svelte';

/** What a host passes to `ArtifactLeaf.svelte`, the pane that shows a workspace's open documents. */
export interface ArtifactLeafProps {
	/** The workspace whose tabs this leaf shows: a chat's id, or Activity's own key. */
	threadId: string | null;
	/** The chat a document is read for (comments, history) when it differs from the workspace key: Activity's items. */
	targetThreadId?: string | null;
	/** A host's status line below the tab strip. */
	notice?: Snippet;
	/** Keep a reply landing visible below the host's notice when the comments list opens. */
	protectReplyLanding?: boolean;
	/** False where the leaf is the whole pane, with no chat to collapse back to. */
	collapsible?: boolean;
	mode: 'rail' | 'expanded' | 'compact';
	/** Compact only: Escape hands the canvas back to chat. */
	escapeCloses?: boolean;
	onEscapeClose?: () => void;
	onOpenLightbox?: (url: string) => void;
}
