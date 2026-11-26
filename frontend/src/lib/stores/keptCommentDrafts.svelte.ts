// The text typed in a new comment's box on a document or an image, with its tags, until the server stores it or the person
// presses Cancel. Closing the box any other way (Escape, a click outside, another card, another tab, a reload) keeps it here,
// and the next box opened on the same document or image starts from it. A post that failed keeps the reason it gave.
// A reply being typed on a document's or an image's thread is kept here the same way (`replyDraftKey`), with its caret, until
// it posts or the person empties its field, so leaving the viewer (another conversation, tab or pane) and coming back finds it.
// It is also kept on this device under the signed-in person (`keptCommentDraftsStorage`), so a reload, a quit or an
// update restart finds it; the person's own Sign out clears it, an expired session does not. From the first press of Comment
// it also holds the draft's one client id, which every later press posts under, so the server never stores it twice.
// A box opened from it keeps that id with it (`origin`), so a box left unedited posts under it even once the draft is gone.
// The same holds for a comment typed on a passage of a chat (`chatDraftKey`) and a reply typed in a chat's comment thread.
import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import { elementOnScreen as onScreen } from '$lib/elementOnScreen';
import { onSignOut, user } from './auth';
import { newClientId } from './documentWire';
import type { KeptPlace } from '$lib/keptPlace';
import { createDraftStorage, type DraftSend, type NotKept, type StoredDraft } from './keptCommentDraftsStorage';
import { toasts } from './toast';

export type MentionSelection = ReturnType<typeof createMentionSelection>;
/**
 * `toast`: the toast up now telling why the draft's post failed (`keptCommentDrafts.toast`), while its box is not on screen.
 * `place`: the passage it was written on, when that passage could be told apart (`uniquePlaceOf`). `send`: what its post
 * needs with no box open, from a press until it posts, is refused or is edited; only a draft holding it is sent without the person.
 */
export type KeptCommentDraft = {
	body: string; mentionSelection: MentionSelection; error?: string; clientId?: string; selection?: [number, number]; toast?: string;
	place?: KeptPlace; send?: DraftSend;
};
/** The kept draft a box opened from, once Comment was pressed on it: its id, and the text and tags it held then. */
export type DraftOrigin = { clientId: string; body: string; tags: string };

/** A draft's tagged agents, compared as a set, as the server stores them. */
const tagsOf = (tags: MentionSelection, body: string) => [...new Set(tags.occurrences(body).map((item) => item.agent_id))].sort().join('\n');
const sameTags = (tags: MentionSelection, body: string, sent: string[]) => tagsOf(tags, body) === [...new Set(sent)].sort().join('\n');

/**
 * One reactive cell per key, so a card reads only its own draft: typing in one reply changes nothing another card
 * reads (a map-wide version would re-run every card without a draft on each keystroke). Raw, so the tags object is
 * kept as it is, not proxied.
 */
class Cell {
	draft = $state.raw<KeptCommentDraft | undefined>();
	/** Why this device does not keep the draft, while it does not; its box says so under the text. */
	notKept = $state<NotKept | null>(null);
	/** The toast saying so, shown because no box of the draft was on screen when it stopped being kept. */
	notKeptToast: string | undefined;
}
const cells = new Map<string, Cell>();
const cell = (key: string): Cell => {
	let found = cells.get(key);
	if (!found) cells.set(key, found = new Cell());
	return found;
};
/** The draft as this device keeps it: text, tags, id, passage and send; an empty box keeps nothing. */
const stored = (draft: KeptCommentDraft | undefined): StoredDraft | null => draft?.body
	? { body: draft.body, tags: draft.mentionSelection.occurrences(draft.body), ...(draft.clientId ? { clientId: draft.clientId } : {}),
		...(draft.place ? { place: draft.place } : {}), ...(draft.send ? { send: draft.send } : {}) }
	: null;
function restored({ body, tags, clientId, place, send }: StoredDraft): KeptCommentDraft {
	const mentionSelection = createMentionSelection(body);
	for (const tag of tags) mentionSelection.select(tag.agent_id, tag.start, body.slice(tag.start, tag.end));
	return { body, mentionSelection, ...(clientId ? { clientId } : {}), ...(place ? { place } : {}), ...(send ? { send } : {}) };
}
/**
 * The draft after its box holds `body`. An edit since a press makes it typed again (no `send`), so words never pressed are
 * never sent without the person; its id stays, so its next press finds an earlier one the server stored. A box on a passage
 * says where the draft is (`place`; null for none); a box that does not know leaves it (undefined).
 */
function edited(prior: KeptCommentDraft | undefined, body: string, mentionSelection: MentionSelection, place?: KeptPlace | null): KeptCommentDraft {
	const next: KeptCommentDraft = { ...prior, body, mentionSelection };
	if (prior?.send && prior.body !== body) delete next.send;
	if (place === null) delete next.place; else if (place) next.place = place;
	return next;
}
/** Memory without storage: the draft goes from this page, and the toasts telling its failure or that it was not kept with it. */
function drop(key: string): void {
	const found = cells.get(key);
	if (found?.draft?.toast) toasts.dismiss(found.draft.toast);
	if (found) { found.draft = undefined; notKept(key, null); }
}
/** What the line under a box says when this device does not keep its draft: what happened, and what keeps the text. */
export const NOT_KEPT_MESSAGE: Record<NotKept, string> = {
	'too-long': 'Too long to keep on this device. Post it or copy it before you close CORSIA.',
	full: 'This device has no room left to keep this draft. Post it or copy it before you close CORSIA.',
	refused: 'Couldn’t keep this draft on this device. Post it or copy it before you close CORSIA.',
};
/**
 * Whether this device keeps the draft changed. Said in one place: the line under its box (which reads `notKept`), or, when
 * no box of it is on screen at that moment, a toast, which goes once the draft is kept again, leaves, or its box comes on screen.
 */
function notKept(key: string, why: NotKept | null): void {
	const found = why ? cell(key) : cells.get(key);
	if (!found || found.notKept === why) return;
	found.notKept = why;
	if (found.notKeptToast) { toasts.dismiss(found.notKeptToast); found.notKeptToast = undefined; }
	if (why && !keptCommentDrafts.onScreen(key)) found.notKeptToast = toasts.show(NOT_KEPT_MESSAGE[why], 'error');
}
/** `id`'s drafts leave memory and nothing more is kept for them: their Sign out, here or in another window. */
function forget(id: string): void {
	if (owner !== id) return;
	for (const key of cells.keys()) drop(key);
	owner = null;
}
const device = createDraftStorage({
	read: (key) => stored(cells.get(key)?.draft),
	// Another window changed it: this one follows, unless the person is typing in its box here.
	changed: (key, draft) => {
		if (focused(key)) return;
		if (draft) cell(key).draft = restored(draft); else drop(key);
	},
	kept: notKept,
	// Even a box the person is typing in: what they typed is theirs, and they signed out.
	signedOut: forget,
});
const kept = {
	get: (key: string) => cell(key).draft,
	set: (key: string, draft: KeptCommentDraft) => { cell(key).draft = draft; device.save(key); },
	/** The draft goes, from this page and this device: what it held is posted, cancelled or cleared. */
	delete: (key: string) => { drop(key); device.remove(key); },
};

/** Whose drafts memory holds: they are kept on this device under that id. */
let owner: string | null = null;
/**
 * Someone is signed in. The same person again (a reconnect, or a sign-in after their session expired) changes nothing.
 * Another person's drafts leave memory (they stay on the device under their id), and this person's come back. Drafts
 * typed before anyone was signed in are this person's.
 */
function signedIn(id: string | null): void {
	if (!id || id === owner) return;
	// What the last person typed is on the device under their id already: every change is written as it happens.
	if (owner) for (const key of cells.keys()) drop(key);
	owner = id;
	for (const [key, draft] of device.signIn(id)) if (!cells.get(key)?.draft) cell(key).draft = restored(draft);
	for (const [key, found] of cells) if (found.draft) device.save(key);
}
user.subscribe((signed) => signedIn(signed?.id ?? null));
/** The person's own Sign out: their drafts go from this page and from this device; another person's stay. */
onSignOut((id) => {
	device.signOut(id);
	forget(id);
});

/**
 * The boxes on the page that show a draft's text and its reason (a new comment's composer, a reply's field, an image's box),
 * by the draft's key. A failed post says why in one place: in such a box when one is on screen at that moment, else in a toast.
 */
const boxes = new Map<string, Set<Element>>();
function register(key: string, box: Element): () => void {
	if (!key) return () => {};
	let found = boxes.get(key);
	if (!found) boxes.set(key, found = new Set());
	found.add(box);
	// A box that slides in is laid out off screen as it mounts, so it is asked once more on the next frame.
	if (!told(key, box) && typeof requestAnimationFrame === 'function') requestAnimationFrame(() => { if (found.has(box)) told(key, box); });
	return () => { found.delete(box); if (!found.size && boxes.get(key) === found) boxes.delete(key); };
}
/**
 * The draft's own box is on screen: if a toast is up telling why its post failed, the box says it now (it shows the kept
 * reason), so the toast goes and the reason is told in one place. True once the box was found on screen.
 */
function told(key: string, box: Element): boolean {
	if (!onScreen(box)) return false;
	const draft = kept.get(key), found = cells.get(key);
	if (draft?.error && draft.toast) {
		toasts.dismiss(draft.toast);
		kept.set(key, { ...draft, toast: undefined });
	}
	if (found?.notKeptToast) { toasts.dismiss(found.notKeptToast); found.notKeptToast = undefined; }
	return true;
}
/** The person is typing in a box showing this draft. */
function focused(key: string): boolean {
	const active = typeof document === 'undefined' ? null : document.activeElement;
	return Boolean(active) && [...(boxes.get(key) ?? [])].some((box) => box.contains(active));
}
/** `use:showsDraft={key}` on a box's root: registered under the draft's key while mounted (`keptCommentDrafts.shown`). */
export function showsDraft(node: Element, key: string) {
	let drop = register(key, node);
	return {
		update(next: string) { drop(); drop = register(next, node); },
		destroy() { drop(); },
	};
}

export { CHAT_REPLY_SCOPE, chatDraftKey, documentDraftKey, imageDraftKey, replyDraftKey } from './commentDraftKeys';

export const keptCommentDrafts = {
	get(key: string): KeptCommentDraft | undefined { return key ? kept.get(key) : undefined; },
	/** The keys of the drafts pressed and not yet posted, refused or edited since (`send`), whose post may go on its own. */
	pressed(): string[] { return [...cells].filter(([, found]) => found.draft?.send).map(([key]) => key); },
	/** What the line under the draft's box says while this device does not keep it (`NOT_KEPT_MESSAGE`), else ''. */
	notKept(key: string): string {
		const why = key ? cell(key).notKept : null;
		return why ? NOT_KEPT_MESSAGE[why] : '';
	},
	/**
	 * A box showing this draft is mounted at `box` until the returned function is called (`showsDraft` calls it on unmount).
	 * Coming on screen with a failure toast up for the draft, it takes the toast's place (`toast`).
	 */
	shown(key: string, box: Element): () => void { return register(key, box); },
	/** A box showing this draft is on the page and on screen now: one read at the moment of asking, nothing observed. */
	onScreen(key: string): boolean {
		for (const box of (key && boxes.get(key)) || []) if (onScreen(box)) return true;
		return false;
	},
	/** The box's text, the tags it holds and its passage (`edited`); an empty box with no failure to show keeps nothing. */
	keep(key: string, body: string, mentionSelection: MentionSelection, place?: KeptPlace | null): void {
		if (!key) return;
		const prior = kept.get(key);
		if (body || prior?.error) kept.set(key, edited(prior, body, mentionSelection, place));
		else kept.delete(key);
	},
	/** A reply's field (`edited`). A reply has no Cancel: emptying its field clears it, reason and id included. */
	keepReply(key: string, body: string, mentionSelection: MentionSelection): void {
		if (!key) return;
		if (body) kept.set(key, edited(kept.get(key), body, mentionSelection));
		else kept.delete(key);
	},
	/** The server refused the draft's post: it is never sent again without the person. */
	unsend(key: string): void {
		const prior = key ? kept.get(key) : undefined;
		if (prior?.send) { const next = { ...prior }; delete next.send; kept.set(key, next); }
	},
	/** Where the caret was in a kept field, so the field opens with it there. */
	select(key: string, selection: [number, number] | null): void {
		const prior = key ? kept.get(key) : undefined;
		if (prior) kept.set(key, { ...prior, selection: selection ?? undefined });
	},
	/**
	 * A post failed: the text stays with the reason, which the box shows until the next press of Comment. With
	 * `freshId` (its earlier version is stored under the old id) the next press posts it as a new comment.
	 */
	fail(key: string, body: string, mentionSelection: MentionSelection, error: string, freshId = false): void {
		const prior = key ? kept.get(key) : undefined;
		if (key) kept.set(key, { body, mentionSelection, error, clientId: freshId ? undefined : prior?.clientId, selection: prior?.selection, toast: prior?.toast, place: prior?.place, send: freshId ? undefined : prior?.send });
	},
	/**
	 * The toast telling why the draft's post failed (`id`), or none because its box says why (null). The draft holds it
	 * until the post succeeds, the draft goes, a later failure replaces it, or the draft's box comes on screen (`shown`):
	 * an earlier one is dismissed, so a failure is never told twice. With no draft kept, there is nothing to hold it.
	 */
	toast(key: string, id: string | null): void {
		const prior = key ? kept.get(key) : undefined;
		if (prior?.toast && prior.toast !== id) toasts.dismiss(prior.toast);
		if (prior) kept.set(key, { ...prior, toast: id ?? undefined });
	},
	/** A new press: the reason goes from the box, and its toast stays until that press ends one way or the other. */
	clearError(key: string): void {
		const prior = key ? kept.get(key) : undefined;
		if (prior?.error) kept.set(key, { body: prior.body, mentionSelection: prior.mentionSelection, clientId: prior.clientId, selection: prior.selection, toast: prior.toast, place: prior.place, send: prior.send });
	},
	/** What a box opening from this draft carries with it: the draft's id once Comment was pressed, with the text and tags it held. */
	origin(key: string): DraftOrigin | undefined {
		const prior = key ? kept.get(key) : undefined;
		return prior?.clientId ? { clientId: prior.clientId, body: prior.body, tags: tagsOf(prior.mentionSelection, prior.body) } : undefined;
	},
	/**
	 * Comment was pressed: the draft's one id, minted on its first press and kept with its text until it posts or is
	 * cancelled. `again` says an earlier press used it, so that press may have stored the comment already. A box opened
	 * from the draft (`origin`) whose post was stored meanwhile, in a viewer since closed, presses under the same id while
	 * its text and tags are as it opened: the server answers with the stored comment. Any edit since posts fresh.
	 */
	claim(key: string, body: string, mentionSelection: MentionSelection, origin?: DraftOrigin, send?: DraftSend): { clientId: string; again: boolean } {
		const prior = key ? kept.get(key) : undefined;
		// What the post needs with no box open is kept from this press, so the draft is sent again on its own if this one fails.
		const sending = send ? { send } : {};
		if (prior?.clientId) {
			if (send && key) kept.set(key, { ...prior, ...sending });
			return { clientId: prior.clientId, again: true };
		}
		if (!prior && origin && origin.body === body && origin.tags === tagsOf(mentionSelection, body)) {
			// Kept under that id, as a first press keeps its own, so a press after this one failing reuses it.
			if (key) kept.set(key, { body, mentionSelection, clientId: origin.clientId, ...sending });
			return { clientId: origin.clientId, again: true };
		}
		const clientId = newClientId();
		if (key) kept.set(key, { ...prior, body, mentionSelection, clientId, ...sending });
		return { clientId, again: false };
	},
	/** Cancel, or the server stored the comment. */
	forget(key: string): void { kept.delete(key); },
	/**
	 * The server stored the draft posted as `sent` with `sentTags`. If it still holds just that, the draft goes and this
	 * says so (true); if its text or tags changed meanwhile, they stay, with their own copy of the tags (the box that
	 * pressed clears its own) and a fresh id for their own post.
	 */
	posted(key: string, sent: string, sentTags: string[]): boolean {
		const prior = key ? kept.get(key) : undefined;
		if (!prior) return false;
		if (prior.body.trim() === sent.trim() && sameTags(prior.mentionSelection, prior.body, sentTags)) { kept.delete(key); return true; }
		if (prior.toast) toasts.dismiss(prior.toast);
		kept.set(key, { body: prior.body, mentionSelection: prior.mentionSelection.copy(), selection: prior.selection, place: prior.place });
		return false;
	},
	/** Tests only: a fresh page. */
	reset(): void { for (const found of cells.values()) { found.draft = undefined; found.notKept = null; found.notKeptToast = undefined; } boxes.clear(); device.reset(); owner = null; },
};
