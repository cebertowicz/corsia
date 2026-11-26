// The post of a pressed comment draft sent with no box open (`unsentComments`): what it sends, built on the same post a
// press makes, under the draft's one id, so the server stores it once whichever sends it.
import type { MentionOccurrence } from '$lib/components/comments/mentionSelection';
import type { KeptCommentDraft } from '$lib/stores/keptCommentDrafts.svelte';
import { commentFields, postTop } from '$lib/stores/docComments';

/** What a press of the draft sends: its text trimmed, and its tags counted from that text, as its box sends them. */
export type PressedVersion = { text: string; taggedAgentIds: string[]; mentions: MentionOccurrence[] };

export function pressedVersion(draft: KeptCommentDraft): PressedVersion {
	const offset = draft.body.length - draft.body.trimStart().length;
	const mentions = draft.mentionSelection.occurrences(draft.body).map((item) => ({ ...item, start: item.start - offset, end: item.end - offset }));
	return { text: draft.body.trim(), taggedAgentIds: [...new Set(mentions.map((item) => item.agent_id))], mentions };
}

const isDocument = (key: string) => key.startsWith('document:');

/**
 * Whether the draft goes without the person: it was pressed (`send`) under its id, and a document's draft has its passage
 * (`place`). A draft whose passage could not be told apart when kept is never sent this way.
 */
export function sendable(key: string, draft: KeptCommentDraft | undefined): draft is KeptCommentDraft & Required<Pick<KeptCommentDraft, 'send' | 'clientId'>> {
	if (!draft?.send || !draft.clientId || !draft.body.trim()) return false;
	return isDocument(key) && Boolean(draft.place);
}

/**
 * Post the draft as its last press did, with its passage's sides, so the server places it by them when no capture is
 * held for it (`kept_place`). Resolves with the version it sent; fails as the press's post fails.
 */
export async function sendUnsent(key: string, draft: KeptCommentDraft): Promise<PressedVersion> {
	if (!sendable(key, draft) || !draft.place) throw new Error('Not a draft that sends on its own.');
	const { send, clientId, place } = draft;
	const version = pressedVersion(draft);
	const fields = commentFields(place.quote, place.from, place.to, version.text, 'you', false, version.taggedAgentIds, send.threadId, version.mentions);
	// An earlier press used this id: a stored comment under it is that press's, checked against this text (`checkVersion`).
	await postTop(send.artifactId, clientId, { ...fields, anchor_prefix: place.prefix, anchor_suffix: place.suffix }, true);
	return version;
}
