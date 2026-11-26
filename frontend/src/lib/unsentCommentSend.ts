// The post of a pressed comment draft sent with no box open (`unsentComments`): what it sends, built on the same post a
// press makes, under the draft's one id, so the server stores it once whichever sends it.
import type { MentionOccurrence } from '$lib/components/comments/mentionSelection';
import type { KeptCommentDraft } from '$lib/stores/keptCommentDrafts.svelte';
import { commentFields, postTop } from '$lib/stores/docComments';
import { keptSides } from '$lib/keptPlace';

/** What a press of the draft sends: its text trimmed, and its tags counted from that text, as its box sends them. */
export type PressedVersion = { text: string; taggedAgentIds: string[]; mentions: MentionOccurrence[] };

export function pressedVersion(draft: KeptCommentDraft): PressedVersion {
	const offset = draft.body.length - draft.body.trimStart().length;
	const mentions = draft.mentionSelection.occurrences(draft.body).map((item) => ({ ...item, start: item.start - offset, end: item.end - offset }));
	return { text: draft.body.trim(), taggedAgentIds: [...new Set(mentions.map((item) => item.agent_id))], mentions };
}

const isDocument = (key: string) => key.startsWith('document:');
/** An image key's image inside a document (`imageDraftKey`): '' for an image artifact, null for a key that is no image's. */
function imageIn(key: string): string | null {
	if (!key.startsWith('image:')) return null;
	try {
		const [, image] = JSON.parse(key.slice('image:'.length)) as [string, string];
		return typeof image === 'string' ? image : null;
	} catch {
		return null;
	}
}

/**
 * Whether the draft goes without the person: it was pressed (`send`) under its id, and it has where it goes. A document's
 * draft needs its passage (`place`); an image's, its area (`region`), and an image inside a document the image's markdown
 * as well (`place`). A draft whose passage could not be told apart when kept is never sent this way.
 */
export function sendable(key: string, draft: KeptCommentDraft | undefined): draft is KeptCommentDraft & Required<Pick<KeptCommentDraft, 'send' | 'clientId'>> {
	if (!draft?.send || !draft.clientId || !draft.body.trim()) return false;
	if (isDocument(key)) return Boolean(draft.place) && !draft.region;
	const image = imageIn(key);
	return image !== null && Boolean(draft.region) && (image === '' ? !draft.place : Boolean(draft.place));
}

/**
 * Post the draft as its last press did, with its passage's sides, so the server places it by them when no capture is
 * held for it (`kept_place`); an image's area goes with the diagram it was drawn on. Resolves with the version it sent;
 * fails as the press's post fails.
 */
export async function sendUnsent(key: string, draft: KeptCommentDraft): Promise<PressedVersion> {
	if (!sendable(key, draft)) throw new Error('Not a draft that sends on its own.');
	const { send, clientId, place, region } = draft;
	const version = pressedVersion(draft);
	const fields = place
		? { ...commentFields(place.quote, place.from, place.to, version.text, 'you', false, version.taggedAgentIds, send.threadId, version.mentions), ...keptSides(place) }
		: { body: version.text, author: 'you' as const, author_kind: 'user' as const, tagged_agent_ids: version.taggedAgentIds, tagged_agent_mentions: version.mentions, thread_id: send.threadId };
	const image = region ? { region, ...(send.diagramSource !== undefined ? { diagram_source: send.diagramSource } : {}) } : {};
	// An earlier press used this id: a stored comment under it is that press's, checked against this text (`checkVersion`).
	await postTop(send.artifactId, clientId, { ...fields, ...image }, true);
	return version;
}
