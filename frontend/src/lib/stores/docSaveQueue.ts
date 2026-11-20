import { get } from 'svelte/store';
import { writeContent } from '$lib/api/docs';
import { closeLiveDocument, liveDocumentBindings } from './liveDocuments';
import type { LiveDocumentSession } from './liveDocumentSession';
import { flushPositions } from '$lib/stores/docComments';
import { toasts } from '$lib/stores/toast';
import { saveErrorCode, saveErrorMessage } from './docSaveError';
import { createBaseDigests, EDITOR_STALE_CODE, EDITOR_STALE_STATUS, staleToast } from './docSaveBase';
import { docSaveBars } from './docSaveBars';
import { createDocSaveActivityProjection } from './docSaveActivity';
export { idleDocSaveActivity, type DocSaveActivity } from './docSaveActivity';
import {
	docSaveDrafts as drafts, draftKey, getDraft, patchDraft, type TargetIdentity,
} from './docSaveDrafts';
export {
	docSaveDrafts, draftKey, enrichDraftTarget, existingDraft, relocateDraftTarget, seedDraft, withDraftArtifactId,
	type DocSaveDraft, type DocSaveStatus,
} from './docSaveDrafts';

const SAVE_DEBOUNCE_MS = 700;

// Per-key bookkeeping kept outside the reactive store — never exposed directly.
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const inflight = new Map<string, Promise<boolean>>();
const needsResave = new Map<string, boolean>();
const reconnectWaits = new Map<string, () => void>();
const confirmationsOwed = new Map<string, true>();
// The base text a save was refused on because the file changed after it. That base is never sent
// again: typing on keeps the draft and its error, and only a new base can save: the person's choice of
// which text to keep (`keepMyText` when it starts, `useFileText`), the only things besides closing that delete it.
const refusedBases = new Map<string, string>();
const baseDigests = createBaseDigests();
const activity = createDocSaveActivityProjection({ inflight, needsResave, reconnectWaits, confirmationsOwed });
export const docSaveActivities = activity.store;
const publishActivity = activity.publish;

/** The cue calls this only after it displays a confirmation owed by a reconnect. */
export function confirmReconnectSave(key: string): void {
	confirmationsOwed.delete(key);
	publishActivity(key);
}

export function setDraftText(target: TargetIdentity, text: string): void {
	const key = draftKey(target);
	const cur = getDraft(key);
	if (!cur || cur.editText === text) return;
	const matchesSaved = text === cur.savedText;
	// A refused base stays an error while typing goes on, so its bar and its Details do not come and go with each key.
	const refused = !matchesSaved && refusedBases.get(key) === cur.savedText;
	patchDraft(key, {
		editText: text,
		status: matchesSaved ? 'idle' : refused ? 'error' : 'dirty',
		error: matchesSaved ? '' : cur.error,
		stale: matchesSaved ? false : cur.stale,
	});
	// A live document re-saves only for its own unacknowledged transactions, read when the flush returns.
	if (inflight.has(key) && !get(liveDocumentBindings)[key]) needsResave.set(key, true);
	publishActivity(key);
	const existingTimer = timers.get(key);
	if (existingTimer) clearTimeout(existingTimer);
	timers.set(key, setTimeout(() => { timers.delete(key); void runSave(key); }, SAVE_DEBOUNCE_MS));
}

function waitForLiveReconnect(key: string, live: LiveDocumentSession): void {
	if (reconnectWaits.has(key)) return;
	const draft = getDraft(key);
	if (draft && draft.editText !== draft.savedText) confirmationsOwed.set(key, true);
	const stop = live.onFrame((frame) => {
		if (frame.type !== 'doc_opened') return;
		const draft = getDraft(key);
		if (get(liveDocumentBindings)[key] !== live || !draft || draft.editText === draft.savedText || !live.writable) {
			reconnectWaits.delete(key);
			if (!draft || draft.editText === draft.savedText) confirmationsOwed.delete(key);
			publishActivity(key);
			stop();
			return;
		}
		reconnectWaits.delete(key);
		publishActivity(key);
		stop();
		void runSave(key);
	});
	reconnectWaits.set(key, stop);
	publishActivity(key);
}

/**
 * Runs (or piggybacks on) exactly one in-flight write per key. This is the
 * serialization that makes reversed network completion impossible: there is
 * never more than one PUT in flight for a given key, so there is never a
 * pair of responses that can arrive "out of order" against each other. Any
 * edit that lands while a write is in flight is coalesced into exactly one
 * follow-up write once the current one settles.
 */
async function runSave(key: string): Promise<boolean> {
	const current = inflight.get(key);
	if (current) return current;
	const draft = getDraft(key);
	const live = get(liveDocumentBindings)[key];
	if (!draft || (live ? !live.writable : draft.editText === draft.savedText)) return true;
	// A live tab writes only its own transactions: once every one is acked and written there is nothing to
	// save, and any remaining difference is remote edits the server already owns, so the draft rebaselines
	// on them. An unacked transaction still flushes, so the resave loop below keeps going until clean.
	if (live && live.durability.saved) {
		if (draft.status === 'dirty') patchDraft(key, { savedText: draft.editText, status: 'idle' });
		return true;
	}
	// The same stale base is refused once, then kept: the draft and its error stay as they are.
	if (!live && refusedBases.get(key) === draft.savedText) {
		patchDraft(key, { status: 'error', error: EDITOR_STALE_STATUS, stale: true });
		return false;
	}
	const textAtStart = draft.editText;
	// The file's text when this write was built on it; taken with the text it writes so the two cannot drift.
	const baseAtStart = draft.savedText;
	patchDraft(key, { status: 'saving' });
	const p = (async () => {
		try {
			const result = live ? await live.flush() : await writeContent(draft.target.path, textAtStart, {
				projectId: draft.target.projectId, threadId: draft.target.threadId,
				expectedSha256: await baseDigests.for(key, baseAtStart),
			});
			// A live flush is settled by this tab's own transactions, never by the file's digest: the remaining
			// difference is remote edits the server owns. Local work that is unacked, or was typed after this
			// flush began and so is not in the file yet, re-saves.
			const saved = !live || live.durability.saved;
			if (!saved) needsResave.set(key, true);
			publishActivity(key);
			const cur = getDraft(key);
			if (cur) {
				const savedText = live ? (saved ? cur.editText : cur.savedText) : textAtStart;
				patchDraft(key, {
					savedText,
					error: '',
					stale: false,
					status: saved && cur.editText === savedText ? 'saved' : 'dirty',
					// The tab that wrote this revision rebaselines on it, so only the
					// same document open elsewhere shows a changed dot.
					savedRevision: result && 'revision' in result ? result.revision ?? cur.savedRevision : cur.savedRevision,
				});
			}
			// The save succeeded; only the notice to other agents failed. Say so
			// once, without turning the draft into an error or retrying anything.
			if (result && 'context_notice_error' in result && typeof result.context_notice_error === 'string' && result.context_notice_error) {
				toasts.show(result.context_notice_error, 'error', 8000);
			}
			if (!live && draft.target.artifactId) void flushPositions(draft.target.artifactId, textAtStart);
		} catch (e) {
			if (live && live.connection !== 'connected' && live.writable) {
				if (getDraft(key)) patchDraft(key, { status: 'dirty', error: '' });
				waitForLiveReconnect(key, live);
				return false;
			}
			console.error('doc save failed', e);
			const stale = !live && saveErrorCode(e) === EDITOR_STALE_CODE;
			if (stale) refusedBases.set(key, baseAtStart);
			const message = stale ? EDITOR_STALE_STATUS : saveErrorMessage(e);
			if (getDraft(key)) patchDraft(key, { status: 'error', error: message, stale });
			// The tab's save bar says it when that bar is on screen; a document closed or hidden behind another tab says it here.
			if (!docSaveBars.onScreen(key)) toasts.show(stale ? staleToast(draft.target.name) : message, 'error');
			return false;
		}
		return true;
	})();
	inflight.set(key, p);
	publishActivity(key);
	const completed = await p;
	inflight.delete(key);
	const resave = needsResave.get(key);
	needsResave.delete(key);
	publishActivity(key);
	return resave ? runSave(key) : completed;
}

/**
 * Cancels any pending debounce and drains the save queue for this exact
 * target — including any edit coalesced in after the flush began — then
 * reports whether it is now safe to close (no unsaved text, no error).
 * Works identically for the active tab and any inactive tab: nothing here
 * depends on which tab is currently mounted in DocViewer.
 */
export async function flushDraft(target: TargetIdentity): Promise<boolean> {
	const key = draftKey(target);
	const timer = timers.get(key);
	if (timer) { clearTimeout(timer); timers.delete(key); }
	while (true) {
		if (!await runSave(key)) return false;
		const draft = getDraft(key);
		if (!draft) return true;
		if (draft.status === 'error') return false;
		// Live: every local transaction is acked and in the file, not only in the server's memory.
		const live = get(liveDocumentBindings)[key];
		if (live ? live.durability.saved || !live.writable : draft.editText === draft.savedText) return true;
	}
}

/**
 * The person's choice to keep their text after a stale refusal. The file text just read becomes the tab's base and the text
 * saves against its digest: the guarded writer, so a file that changed again is refused again (the refusal and the text stay).
 * Resolves true when the text is saved.
 */
export async function keepMyText(target: TargetIdentity, fileText: string): Promise<boolean> {
	const key = draftKey(target);
	const cur = getDraft(key);
	if (!cur) return false;
	// The new base is the file's own text, which the old refusal must not catch if the file went back to the refused text.
	refusedBases.delete(key);
	// The refusal stays up while the write runs, so its bar and its Details do not close and reopen; the save's own result clears or renews it.
	// Nothing to write when the file already reads as his text: that ends the refusal here.
	const same = cur.editText === fileText;
	patchDraft(key, { savedText: fileText, status: same ? 'idle' : 'dirty', ...(same ? { error: '', stale: false } : {}) });
	return flushDraft(target);
}

/**
 * The person's choice to use the file's text after a stale refusal: the tab's base and text become the file's and the
 * refusal ends. The viewer shows that text through its one content writer, right after this returns.
 */
export function useFileText(target: TargetIdentity, file: { text: string; revision: string | null }): void {
	const key = draftKey(target);
	const cur = getDraft(key);
	if (!cur) return;
	const timer = timers.get(key);
	if (timer) { clearTimeout(timer); timers.delete(key); }
	needsResave.delete(key);
	refusedBases.delete(key);
	patchDraft(key, { editText: file.text, savedText: file.text, status: 'idle', error: '', stale: false, savedRevision: file.revision ?? cur.savedRevision });
	publishActivity(key);
}

/** Removes a key's draft once its tab has actually closed. Close callers
 * drain the target queue first, so this only removes fully settled state. */
export function clearDraft(target: TargetIdentity): void {
	const key = draftKey(target);
	const timer = timers.get(key);
	if (timer) clearTimeout(timer);
	timers.delete(key);
	needsResave.delete(key);
	refusedBases.delete(key);
	baseDigests.clear(key);
	reconnectWaits.get(key)?.();
	reconnectWaits.delete(key);
	confirmationsOwed.delete(key);
	activity.clear(key);
	drafts.update((all) => {
		if (!(key in all)) return all;
		const next = { ...all };
		delete next[key];
		return next;
	});
}

export interface CloseGateCallbacks {
	onClose: () => void;
	onBringIntoView: () => void;
}

/**
 * Flushes a target's pending save and only proceeds with `onClose` if the
 * flush succeeds; on failure, calls `onBringIntoView` instead so the failed
 * edit and its error stay visible and recoverable rather than disappearing
 * with the closed tab. Pure orchestration — no DOM/component dependency —
 * so it works the same whether the target's tab is currently active or not.
 */
export async function closeTabWithSaveGate(
	target: TargetIdentity,
	callbacks: CloseGateCallbacks,
): Promise<{ closed: boolean }> {
	const ok = await flushDraft(target);
	if (!ok) {
		callbacks.onBringIntoView();
		return { closed: false };
	}
	clearDraft(target);
	// 4.1: the close sync and doc_close go now; closing never waits on their ack.
	closeLiveDocument(target.instanceId);
	callbacks.onClose();
	return { closed: true };
}
