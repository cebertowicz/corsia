import type { HeldThreadFeedback } from '$lib/api/docFeedback';

/** Fired by the chat's Send once it has closed document-held comments, so open documents read what they still hold. */
export const THREAD_SENT_EVENT = 'doc-feedback-thread-sent';

const comments = (count: number) => `${count} comment${count === 1 ? '' : 's'}`;

export interface ChatSend {
	count: number;
	label: string;
}

/**
 * The document-held Send the chat offers, decided by which artifacts hold the comments and whether one of them has a
 * visible header Send of its own, never by comparing counts: equal counts on different artifacts are different batches.
 *
 * - nothing held: no Send
 * - exactly one held artifact, and its document's header is visible: that header is the sender, so none here
 * - exactly one held artifact that is not visible: `Send N comments`
 * - several held artifacts (a visible one included): `Send all N comments`
 */
export function chatSend(held: HeldThreadFeedback | null, visibleArtifactId: string | null): ChatSend | null {
	const artifacts = held?.artifacts.filter((artifact) => artifact.open_count > 0) ?? [];
	if (artifacts.length === 0) return null;
	const count = artifacts.reduce((sum, artifact) => sum + artifact.open_count, 0);
	if (artifacts.length === 1) {
		return artifacts[0].artifact_id === visibleArtifactId ? null : { count, label: `Send ${comments(count)}` };
	}
	return { count, label: `Send all ${comments(count)}` };
}

/**
 * The document whose header Send is on screen: the active tab of an expanded canvas, with no image covering it.
 * A collapsed canvas, another active tab, a compact canvas that gives way to chat, or an open image all return null.
 */
export function visibleDocumentArtifactId(canvas: {
	expanded: boolean;
	activeTabKind: string | null;
	artifactId: string | null;
	imageOpen: boolean;
}): string | null {
	if (!canvas.expanded || canvas.activeTabKind !== 'document' || canvas.imageOpen) return null;
	return canvas.artifactId;
}
