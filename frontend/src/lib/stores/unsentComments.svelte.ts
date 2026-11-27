// Comments the person pressed Comment on that have not posted yet go on their own (`unsentCommentSend`), under each
// draft's one id, so the server stores each once. A try is made when the person opens the draft's document (`open`), when
// the live connection comes up (the app's first after a load, and each after a drop) and when they return to the app.
// There is no timer: a try that fails for a reason that passes waits for the next of those, and one refused stops for good.
// One post goes at a time across the app, so these never take the page's own connections from it.
import { get } from 'svelte/store';
import { SERVER_SAVE_MS } from '$lib/api/boundedRequest';
import { user } from './auth';
import { commentDraft } from './commentDraft';
import { posting } from './commentPostTiming';
import { connectionState } from './connection';
import { keptCommentDrafts } from './keptCommentDrafts.svelte';
import { toasts } from './toast';
import { withoutTextKept } from './draftKept';
import { isChat, replyOf, sendable, sendUnsent } from '$lib/unsentCommentSend';
import { unsentOutcome, unsentReason, unsentRetryCode } from '$lib/unsentCommentOutcome';
import { CommentSendRefused } from '$lib/chatCommentAnchors';
import { chatCommentDrafts } from '$lib/chatCommentDrafts.svelte';
import { CHAT_REPLY_SCOPE } from './commentDraftKeys';

/** What a pressed comment's box, or a pressed reply's line, reads while it goes on its own. */
export const SENDING = 'Sending…';
/** A return to the app tries a draft again only this long after its last try, so quick tab switching does not resend it. */
export const RETURN_THROTTLE_MS = SERVER_SAVE_MS;

/** The draft being posted now, if any: one at a time. */
let current: string | null = null;
/** The drafts the triggers so far asked for, in order; a draft asked for twice before its try is tried once. */
const waiting: string[] = [];
/** When each draft was last tried, for the return-to-app throttle. */
const lastTry = new Map<string, number>();
let pumping: Promise<void> | null = null;

/**
 * The name of the draft's document or image, for a notice when no box of it is on screen: the one its press kept, else its
 * key's path; a reply's, the document or image its thread is on. A chat's is its title, kept at its press; one with no
 * title kept reads as the sidebar names an untitled chat.
 */
function nameOf(key: string, kept: string | undefined): string {
	if (kept) return kept;
	if (onChat(key)) return 'New Chat';
	const reply = replyOf(key);
	if (reply) return nameOf(reply.scope, undefined);
	if (!key.startsWith('document:')) return 'an image';
	try {
		const [, path] = JSON.parse(key.slice(key.indexOf(':') + 1)) as [string, string];
		return path.split('/').pop() || path;
	} catch {
		return 'a document';
	}
}
/** Its first few words, so a notice says which comment it is. */
function firstWords(body: string): string {
	const words = body.trim().split(/\s+/);
	return words.length > 6 ? `${words.slice(0, 6).join(' ')}…` : words.join(' ');
}

/** A chat's new comment or a reply in a chat's comment thread: its box is chat's own (`chatCommentDrafts`). */
const onChat = (key: string) => isChat(key) || replyOf(key)?.scope === CHAT_REPLY_SCOPE;

/** It posted: an unedited draft goes, and its box with it, as when the person's own press posts. */
function posted(key: string, text: string, taggedAgentIds: string[]): void {
	if (!keptCommentDrafts.posted(key, text, taggedAgentIds)) return;
	if (onChat(key)) chatCommentDrafts.wentOnItsOwn(key);
	else commentDraft.update((value) => value?.key === key ? null : value);
}

/**
 * It will not go as it is, or an earlier version went: the draft keeps its words with the reason and is never sent again
 * without the person. The reason shows in its box when one is on screen; otherwise a notice names the document, once,
 * without the box's word that the text stays there.
 */
function stopped(key: string, message: string, freshId: boolean, final: boolean): void {
	const draft = keptCommentDrafts.get(key);
	if (!draft) return;
	keptCommentDrafts.fail(key, draft.body, draft.mentionSelection, message, freshId);
	keptCommentDrafts.unsend(key);
	if (onChat(key)) chatCommentDrafts.refused(key, message, final);
	else commentDraft.update((value) => value?.key === key ? { ...value, error: message } : value);
	const toast = keptCommentDrafts.onScreen(key) ? null : toasts.show(`Your comment on ${nameOf(key, draft.send?.name)}, “${firstWords(draft.body)}”: ${withoutTextKept(message)}`, 'error');
	keptCommentDrafts.toast(key, toast);
}

/** Its id is another comment's: it takes a new one and goes once more. */
function renewed(key: string): boolean {
	const draft = keptCommentDrafts.get(key);
	if (!draft?.send) return false;
	const send = draft.send;
	keptCommentDrafts.fail(key, draft.body, draft.mentionSelection, '', true);
	keptCommentDrafts.claim(key, draft.body, draft.mentionSelection, undefined, send);
	return true;
}

async function attempt(key: string, again = false): Promise<void> {
	const draft = keptCommentDrafts.get(key);
	// A press of it in its box is out now: that post is its try.
	if (!get(user) || !sendable(key, draft) || posting(draft.clientId)) return;
	lastTry.set(key, Date.now());
	try {
		const sent = await sendUnsent(key, draft);
		posted(key, sent.text, sent.taggedAgentIds);
	} catch (error) {
		const outcome = unsentOutcome(error);
		if (outcome === 'retry') {
			console.warn('[comments] automatic retry deferred', unsentRetryCode(error));
			return;
		}
		if (outcome === 'fresh' && !again && renewed(key)) return attempt(key, true);
		stopped(key, unsentReason(error), outcome === 'earlier', error instanceof CommentSendRefused && error.final);
	}
}

async function pump(): Promise<void> {
	for (let key = waiting.shift(); key !== undefined; key = waiting.shift()) {
		current = key;
		try { await attempt(key); } finally { current = null; }
	}
	pumping = null;
}

/** Ask for a try of each of `keys` still pressed; with `throttled`, not of one tried in the last RETURN_THROTTLE_MS. */
function ask(keys: string[], throttled = false): void {
	const now = Date.now();
	for (const key of keys) {
		if (key === current || waiting.includes(key) || !sendable(key, keptCommentDrafts.get(key))) continue;
		if (throttled && now - (lastTry.get(key) ?? -Infinity) < RETURN_THROTTLE_MS) continue;
		waiting.push(key);
	}
	if (waiting.length && !pumping) pumping = pump();
}

let connected = false;
connectionState.subscribe((state) => {
	const now = state === 'connected';
	if (now && !connected) ask(keptCommentDrafts.pressed());
	connected = now;
});
if (typeof window !== 'undefined') {
	const returned = () => { if (document.visibilityState === 'visible') ask(keptCommentDrafts.pressed(), true); };
	document.addEventListener('visibilitychange', returned);
	window.addEventListener('focus', returned);
}

export const unsentComments = {
	/** The person opened the draft's document: it is tried now if it is pressed. */
	open(key: string): void { if (key) ask([key]); },
	/** Its box reads "Sending…": it was pressed, and goes on its own until it posts, is refused or is edited. */
	sending(key: string): boolean { return sendable(key, keptCommentDrafts.get(key)); },
	/** A try of it was asked for or made on this page: its post may be out, so only the server's answer settles it. */
	started(key: string): boolean { return key === current || waiting.includes(key) || lastTry.has(key); },
	/** Tests only: every try asked for so far has ended. */
	async settled(): Promise<void> { while (pumping) await pumping; },
	/** Tests only: a fresh page. */
	reset(): void { waiting.length = 0; lastTry.clear(); current = null; pumping = null; connected = false; },
};
