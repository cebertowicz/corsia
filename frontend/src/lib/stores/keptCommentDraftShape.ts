// What `keptCommentDrafts` holds for one draft, and how it reads back from this device and changes as its box is edited.
import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import type { ImageRegion } from '$lib/api/docs';
import type { KeptPlace } from '$lib/keptPlace';
import type { DraftSend, StoredDraft } from './keptCommentDraftsStorage';

export type MentionSelection = ReturnType<typeof createMentionSelection>;
/**
 * `toast`: the toast up now telling why the draft's post failed (`keptCommentDrafts.toast`), while its box is not on screen.
 * `place`: the passage it was written on, when that passage could be told apart (`uniquePlaceOf`); for an image inside a
 * document, the image's markdown. `region`: the area of an image it was drawn on. `send`: what its post needs with no box
 * open, from a press until it posts, is refused or is edited; only a draft holding it is sent without the person.
 */
export type KeptCommentDraft = {
	body: string; mentionSelection: MentionSelection; error?: string; clientId?: string; selection?: [number, number]; toast?: string;
	place?: KeptPlace; region?: ImageRegion; send?: DraftSend;
};
/** The kept draft a box opened from, once Comment was pressed on it: its id, and the text and tags it held then. */
export type DraftOrigin = { clientId: string; body: string; tags: string };
/** Where a box's draft is: its passage, the image area, or both; null for neither, undefined when the box does not know. */
export type DraftWhere = { place?: KeptPlace | null; region?: ImageRegion | null };

/** A draft's tagged agents, compared as a set, as the server stores them. */
export const tagsOf = (tags: MentionSelection, body: string) => [...new Set(tags.occurrences(body).map((item) => item.agent_id))].sort().join('\n');
export const sameTags = (tags: MentionSelection, body: string, sent: string[]) => tagsOf(tags, body) === [...new Set(sent)].sort().join('\n');

/** The draft as this device keeps it: text, tags, id, passage, area and send; an empty box keeps nothing. */
export const stored = (draft: KeptCommentDraft | undefined): StoredDraft | null => draft?.body
	? { body: draft.body, tags: draft.mentionSelection.occurrences(draft.body), ...(draft.clientId ? { clientId: draft.clientId } : {}),
		...(draft.place ? { place: draft.place } : {}), ...(draft.region ? { region: draft.region } : {}), ...(draft.send ? { send: draft.send } : {}) }
	: null;

/** A draft read back from this device. */
export function restored({ body, tags, clientId, place, region, send }: StoredDraft): KeptCommentDraft {
	const mentionSelection = createMentionSelection(body);
	for (const tag of tags) mentionSelection.select(tag.agent_id, tag.start, body.slice(tag.start, tag.end));
	return { body, mentionSelection, ...(clientId ? { clientId } : {}), ...(place ? { place } : {}), ...(region ? { region } : {}), ...(send ? { send } : {}) };
}

/**
 * The draft after its box holds `body`. An edit since a press makes it typed again (no `send`), so words never pressed are
 * never sent without the person; its id stays, so its next press finds an earlier one the server stored. A box says where
 * the draft is (`where`: null for nowhere); what a box does not know (undefined) stays as it was.
 */
export function edited(prior: KeptCommentDraft | undefined, body: string, mentionSelection: MentionSelection, where: DraftWhere = {}): KeptCommentDraft {
	const next: KeptCommentDraft = { ...prior, body, mentionSelection };
	if (prior?.send && prior.body !== body) delete next.send;
	if (where.place === null) delete next.place; else if (where.place) next.place = where.place;
	if (where.region === null) delete next.region; else if (where.region) next.region = where.region;
	return next;
}
