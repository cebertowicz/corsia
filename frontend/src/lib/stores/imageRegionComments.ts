import { patchComment, type ImageRegion } from '$lib/api/docs';
import { docComments, postTop, regionFromServer } from './docComments';
import { newClientId } from './documentWire';
import { selectComment } from './commentSelection';
import { postLive } from './liveCommentPost';
import type { LiveDocumentSession } from './liveDocumentSession';

/** A press of Comment on an image's draft: its one id, and whether an earlier press used it (`keptCommentDrafts.claim`). */
export type ImagePress = { clientId: string; again: boolean };

/** Comment on part of an image artifact: a box, or a point when its width and height are 0. */
export async function addRegionComment(artifactId: string, region: ImageRegion, text: string, taggedAgentIds: string[] = [], threadId: string | null = null, mentions: import('$lib/components/comments/mentionSelection').MentionOccurrence[] = [], press?: ImagePress): Promise<string> {
	return postTop(artifactId, press?.clientId ?? newClientId(), { body: text, region, author: 'you', author_kind: 'user', tagged_agent_ids: taggedAgentIds, tagged_agent_mentions: mentions, thread_id: threadId }, press?.again);
}

/** An image inside a document, by its markdown range in the text on screen. */
export interface DocImageAnchor {
	artifactId: string;
	from: number;
	to: number;
	/** The document's text on screen; its markdown at `from`..`to` is the quote. */
	text: string;
	diagramSource?: string;
	/** A live document tracks the range itself, as it does for a selected quote. */
	session?: LiveDocumentSession;
}

let captures = 0;

/**
 * Comment on part of an image inside a document (DCOL-FR13.3): a text comment anchored on the image's markdown,
 * captured like a selection so it follows the image through edits, plus the region. The server finds the file.
 */
export async function addDocImageComment(image: DocImageAnchor, region: ImageRegion, body: string, taggedAgentIds: string[] = [], threadId: string | null = null, mentions: import('$lib/components/comments/mentionSelection').MentionOccurrence[] = [], press: ImagePress = { clientId: newClientId(), again: false }): Promise<string> {
	const { session } = image;
	const fields = { body, region, diagram_source: image.diagramSource, author: 'you', author_kind: 'user' as const, tagged_agent_ids: taggedAgentIds, tagged_agent_mentions: mentions, thread_id: threadId };
	if (!session) {
		return postTop(image.artifactId, press.clientId, { ...fields, anchor_text: image.text.slice(image.from, image.to), from_off: image.from, to_off: image.to }, press.again);
	}
	const live = { selection: selectComment(session, image.from, image.to), ...press, seq: ++captures, signal: new AbortController().signal, version: { body, tagged_agent_ids: taggedAgentIds } };
	return postLive(live, (captured) => postTop(session.artifactId, captured.clientId, {
		...fields, anchor_text: captured.quote, from_off: captured.from, to_off: captured.to, live_capture: true,
	}, press.again));
}

/** Move a pin. The server places it on the image as it is now, so it is no longer stale. */
export async function moveRegion(artifactId: string, commentId: string, region: ImageRegion, diagramSource?: string): Promise<void> {
	const row = await patchComment(commentId, { region, diagram_source: diagramSource });
	docComments.update((all) => ({
		...all,
		[artifactId]: (all[artifactId] ?? []).map((c) => (c.id === commentId ? { ...c, ...regionFromServer(row) } : c))
	}));
}
