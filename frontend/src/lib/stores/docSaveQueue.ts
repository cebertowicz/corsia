import { writable, get } from 'svelte/store';
import { writeContent } from '$lib/api/docs';
import { closeLiveDocument, liveDocumentBindings } from './liveDocuments';
import type { LiveDocumentSession } from './liveDocumentSession';
import { flushPositions } from '$lib/stores/docComments';
import { toasts } from '$lib/stores/toast';
import { LiveOperationError } from './documentFailures';
import type { DocViewerTarget } from '$lib/docViewerTarget';

export type DocSaveStatus = 'idle' | 'dirty' | 'saving' | 'saved' | 'error';

export interface DocSaveDraft {
	target: DocViewerTarget;
	editText: string;
	savedText: string;
	status: DocSaveStatus;
	error: string;
	/** Revision the server reported for this draft's last successful write. */
	savedRevision: string | null;
}

type TargetIdentity = Pick<DocViewerTarget, 'instanceId'>;

const SAVE_DEBOUNCE_MS = 700;

/**
 * The owning tab instance — never a global per-user or per-artifact key.
 *
 * The same document open in another thread is a different tab instance and so
 * a fully independent draft; and because the key never changes, resolving an
 * artifact id or relocating the file cannot strand an unsaved edit.
 */
export function draftKey(target: TargetIdentity): string {
	return target.instanceId;
}

const drafts = writable<Record<string, DocSaveDraft>>({});
export const docSaveDrafts = drafts;

// Per-key bookkeeping kept outside the reactive store — never exposed directly.
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const inflight = new Map<string, Promise<boolean>>();
const needsResave = new Map<string, boolean>();
const reconnectWaits = new Map<string, () => void>();

function getDraft(key: string): DocSaveDraft | undefined {
	return get(drafts)[key];
}
function patchDraft(key: string, patch: Partial<DocSaveDraft>): void {
	drafts.update((all) => {
		const cur = all[key];
		if (!cur) return all;
		return { ...all, [key]: { ...cur, ...patch } };
	});
}

export function existingDraft(target: TargetIdentity): DocSaveDraft | undefined {
	return getDraft(draftKey(target));
}

/** The draft's target, which carries any identity resolved since it was seeded. */
export function effectiveDraftTarget(target: DocViewerTarget): DocViewerTarget {
	return existingDraft(target)?.target ?? target;
}

/** Establishes a fresh draft from server content. Never overwrites an
 * existing unsaved (dirty/saving/error) draft — the whole point is that a
 * background re-load can never silently discard local edits. */
export function seedDraft(target: DocViewerTarget, serverText: string, revision: string | null = null): void {
	const key = draftKey(target);
	const cur = getDraft(key);
	if (cur && cur.editText !== cur.savedText) return;
	drafts.update((all) => ({
		...all,
		[key]: {
			target, editText: serverText, savedText: serverText,
			status: 'idle', error: '', savedRevision: revision,
		},
	}));
}

/** Records a server-resolved artifactId against an already-seeded draft. */
export function enrichDraftTarget(target: TargetIdentity, artifactId: string): void {
	const key = draftKey(target);
	const cur = getDraft(key);
	if (!cur || cur.target.artifactId) return;
	patchDraft(key, { target: { ...cur.target, artifactId } });
}

/** Follows a document that moved on disk, keeping this draft's unsaved text. */
export function relocateDraftTarget(target: TargetIdentity, path: string): void {
	const key = draftKey(target);
	const cur = getDraft(key);
	if (!cur || cur.target.path === path) return;
	patchDraft(key, { target: { ...cur.target, path } });
}

export function setDraftText(target: TargetIdentity, text: string): void {
	const key = draftKey(target);
	const cur = getDraft(key);
	if (!cur || cur.editText === text) return;
	const matchesSaved = text === cur.savedText;
	patchDraft(key, {
		editText: text,
		status: matchesSaved ? 'idle' : 'dirty',
		error: matchesSaved ? '' : cur.error,
	});
	// A live document re-saves only for its own unacknowledged transactions, read when the flush returns.
	if (inflight.has(key) && !get(liveDocumentBindings)[key]) needsResave.set(key, true);
	const existingTimer = timers.get(key);
	if (existingTimer) clearTimeout(existingTimer);
	timers.set(key, setTimeout(() => { timers.delete(key); void runSave(key); }, SAVE_DEBOUNCE_MS));
}

function waitForLiveReconnect(key: string, live: LiveDocumentSession): void {
	if (reconnectWaits.has(key)) return;
	const stop = live.onFrame((frame) => {
		if (frame.type !== 'doc_opened') return;
		const draft = getDraft(key);
		if (get(liveDocumentBindings)[key] !== live || !draft || draft.editText === draft.savedText || !live.writable) {
			reconnectWaits.delete(key);
			stop();
			return;
		}
		reconnectWaits.delete(key);
		stop();
		void runSave(key);
	});
	reconnectWaits.set(key, stop);
}

function saveErrorMessage(error: unknown): string {
	if (error instanceof LiveOperationError && (error.kind === 'publication_failed' || error.kind === 'setup_failed')) return error.message;
	const raw = error instanceof Error ? error.message : String(error);
	const body = raw.replace(/^\d+\s+/, '');
	try {
		const parsed = JSON.parse(body);
		const detail = parsed?.detail;
		if (detail && typeof detail === 'object') {
			if (typeof detail.line === 'string') return detail.line;
			const message = typeof detail.message === 'string' ? detail.message : "Couldn't save the document";
			const errors = Array.isArray(detail.errors)
				? detail.errors.filter((item: unknown): item is string => typeof item === 'string')
				: [];
			return errors.length ? `${message}: ${errors.join('; ')}` : message;
		}
		if (typeof detail === 'string') return detail;
	} catch {
		// Use the local-edits-safe fallback below for non-JSON failures.
	}
	return "Couldn't save the document — edits are still local";
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
	// A live tab writes only its own transactions; with none unwritten there is nothing to save, and any
	// remaining difference is remote edits the server already owns, so the draft rebaselines on them.
	if (live && !live.durability.unwritten) {
		if (draft.status === 'dirty') patchDraft(key, { savedText: draft.editText, status: 'idle' });
		return true;
	}
	const textAtStart = draft.editText;
	patchDraft(key, { status: 'saving' });
	const p = (async () => {
		try {
			const result = live ? await live.flush() : await writeContent(draft.target.path, textAtStart, {
				projectId: draft.target.projectId, threadId: draft.target.threadId,
			});
			// A live flush is settled by 4.1 cleanliness, never by the file's digest: a clean tab's
			// remaining difference is remote edits the server owns, so only unacked local work re-saves.
			const clean = !live || live.durability.clean;
			if (!clean) needsResave.set(key, true);
			const cur = getDraft(key);
			if (cur) {
				const savedText = live ? (clean ? cur.editText : cur.savedText) : textAtStart;
				patchDraft(key, {
					savedText,
					error: '',
					status: clean && cur.editText === savedText ? 'saved' : 'dirty',
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
			const message = saveErrorMessage(e);
			if (getDraft(key)) patchDraft(key, { status: 'error', error: message });
			toasts.show(message, 'error');
			return false;
		}
		return true;
	})();
	inflight.set(key, p);
	const completed = await p;
	inflight.delete(key);
	const resave = needsResave.get(key);
	needsResave.delete(key);
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
		// Live: clean (an ack names a message after its latest local transaction) and one flush returned.
		const live = get(liveDocumentBindings)[key];
		if (live ? live.durability.clean || !live.writable || !live.durability.unwritten : draft.editText === draft.savedText) return true;
	}
}

/** Removes a key's draft once its tab has actually closed. Close callers
 * drain the target queue first, so this only removes fully settled state. */
export function clearDraft(target: TargetIdentity): void {
	const key = draftKey(target);
	const timer = timers.get(key);
	if (timer) clearTimeout(timer);
	timers.delete(key);
	needsResave.delete(key);
	reconnectWaits.get(key)?.();
	reconnectWaits.delete(key);
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
