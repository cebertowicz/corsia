// Where `keptCommentDrafts` keeps a draft on this device, so it outlives a reload, a quit and an update restart: one
// browser storage entry per draft, under the signed-in person's id. Only `keptCommentDrafts` calls this, so a draft has
// one writer. Stored: the text, its tagged agents, its one client id, the passage it was written on and, once pressed, what
// its post needs; never the caret or why a post failed.
import type { MentionOccurrence } from '$lib/components/comments/mentionSelection';
import type { KeptPlace } from '$lib/keptPlace';

/** What a pressed draft's post needs once no box holds it: kept from its press until it posts, is refused or is edited. */
export type DraftSend = { artifactId: string; threadId: string | null };
export type StoredDraft = { body: string; tags: MentionOccurrence[]; clientId?: string; place?: KeptPlace; send?: DraftSend };

export const DRAFT_STORAGE_PREFIX = 'corsia:commentDraft:v1:';
/**
 * Storage counts a string in UTF-16 code units, key and value both. Chromium gives an origin 5,241,856 of them,
 * whatever the script (measured). The drafts of every person on this device take at most 2,000,000, so the others
 * using this origin's storage keep the rest; one draft takes at most a tenth of that, so one very long draft (the server
 * sets no limit on a comment's length) still leaves room for nine more as long, and never pushes out one stored already.
 */
export const DRAFTS_MAX_CHARS = 2_000_000;
export const DRAFT_MAX_CHARS = 200_000;
/** Why a draft is not kept on this device: longer than one draft may be, the drafts' share is full, or storage refused it. */
export type NotKept = 'too-long' | 'full' | 'refused';
/** What the line under a box says when this device does not keep its draft: what happened, and what keeps the text. */
export const NOT_KEPT_MESSAGE: Record<NotKept, string> = {
	'too-long': 'Too long to keep on this device. Post it or copy it before you close CORSIA.',
	full: 'This device has no room left to keep this draft. Post it or copy it before you close CORSIA.',
	refused: 'Couldn’t keep this draft on this device. Post it or copy it before you close CORSIA.',
};
/** Written and removed in one go by a Sign out, so every other window of this origin hears whose drafts went. */
export const SIGN_OUT_KEY = 'corsia:commentDraftSignOut:v1';

const isText = (value: unknown): value is string => typeof value === 'string';
const isOffset = (value: unknown): value is number => Number.isInteger(value) && (value as number) >= 0;
/** A stored passage, or nothing when it is not one: the draft then comes back with its words and no place. */
function parsePlace(value: unknown): KeptPlace | undefined {
	const place = value as Partial<KeptPlace> | null | undefined;
	if (!place || !isOffset(place.from) || !isOffset(place.to) || place.to < place.from) return undefined;
	if (!isText(place.quote) || !place.quote || !isText(place.prefix) || !isText(place.suffix)) return undefined;
	return { from: place.from, to: place.to, quote: place.quote, prefix: place.prefix, suffix: place.suffix };
}
/** A stored send, or nothing when it is not one: the draft then comes back typed, and waits for a press. */
function parseSend(value: unknown): DraftSend | undefined {
	const send = value as Partial<DraftSend> | null | undefined;
	if (!send || !isText(send.artifactId) || !send.artifactId || !(send.threadId === null || isText(send.threadId))) return undefined;
	return { artifactId: send.artifactId, threadId: send.threadId };
}

/**
 * A stored entry read back, or null when it is not one this version wrote (sign-in removes it). An entry from before a
 * draft kept its passage reads as it did; a place or a send that does not read goes alone, and the words stay.
 */
export function parseStoredDraft(value: string | null): StoredDraft | null {
	if (!value) return null;
	try {
		const parsed = JSON.parse(value) as Partial<StoredDraft>;
		const body = parsed?.body, tags = parsed?.tags, clientId = parsed?.clientId;
		if (typeof body !== 'string' || !body || !Array.isArray(tags)) return null;
		if (clientId !== undefined && typeof clientId !== 'string') return null;
		const valid = tags.every((tag) => tag && typeof tag.agent_id === 'string' && Number.isInteger(tag.start) && Number.isInteger(tag.end)
			&& tag.start >= 0 && tag.start < tag.end && tag.end <= body.length);
		if (!valid) return null;
		const place = parsePlace(parsed.place), send = parseSend(parsed.send);
		return {
			body, tags: tags.map(({ agent_id, start, end }) => ({ agent_id, start, end })),
			...(clientId ? { clientId } : {}), ...(place ? { place } : {}), ...(send ? { send } : {}),
		};
	} catch {
		return null;
	}
}

type Options = {
	/** The draft under `key` as it is in memory now, or null when there is nothing to keep. */
	read(key: string): StoredDraft | null;
	/** Another window of this origin changed or removed the signed-in person's draft under `key`. */
	changed(key: string, draft: StoredDraft | null): void;
	/** The draft under `key` is kept on this device (null) or is not, and why: told on each write that changes it. */
	kept(key: string, notKept: NotKept | null): void;
	/** `id` pressed Sign out in another window of this origin: that person's drafts leave this one too. */
	signedOut(id: string): void;
};

export function createDraftStorage({ read, changed, kept, signedOut }: Options) {
	let owner: string | null = null;
	/** The last value this window wrote or read for each of the owner's drafts, so an unchanged draft is not written again. */
	const written = new Map<string, string>();
	/** The size of every stored draft on this device, every person's, so the cap is checked without reading them back. */
	const sizes = new Map<string, number>();
	let listening = false;

	const storage = (): Storage | null => {
		try { return typeof localStorage === 'undefined' ? null : localStorage; } catch { return null; }
	};
	const entry = (key: string) => `${DRAFT_STORAGE_PREFIX}${owner}:${key}`;
	const total = () => { let sum = 0; for (const size of sizes.values()) sum += size; return sum; };
	/**
	 * A draft is written on every change, with no wait, so nothing typed is lost to a quit or a crash. Each draft is its own
	 * entry, so a keystroke writes one draft: at the 200,000-unit cap one write (JSON.stringify and setItem) measured a median
	 * of 0.2 ms, p95 0.3 to 0.4 ms and max 0.4 to 0.5 ms (Chromium, two runs of 60 writes per size, 2026-11-20).
	 */
	function write(key: string): void {
		const store = storage();
		if (!store || !owner) return;
		const draft = read(key), name = entry(key);
		if (!draft) { remove(key); return; }
		const value = JSON.stringify(draft);
		if (written.get(key) === value) return;
		const size = name.length + value.length;
		// Nothing kept already is pushed out to make room: over a cap, this draft is the one not kept.
		let notKept: NotKept | null = size > DRAFT_MAX_CHARS ? 'too-long' : total() - (sizes.get(name) ?? 0) + size > DRAFTS_MAX_CHARS ? 'full' : null;
		if (!notKept) {
			try {
				store.setItem(name, value);
				written.set(key, value); sizes.set(name, size);
				return kept(key, null);
			} catch {
				notKept = 'refused';
			}
		}
		// The draft in memory is the one that counts: a stored copy of an earlier version would come back as stale text.
		remove(key);
		kept(key, notKept);
	}
	function remove(key: string): void {
		const store = storage();
		if (!store || !owner) return;
		const name = entry(key);
		try { store.removeItem(name); } catch { /* storage refused: nothing more this window can do */ }
		written.delete(key); sizes.delete(name);
	}
	/** Nothing more is written for `id` here: their drafts left the device (their Sign out, here or in another window). */
	function release(id: string): void {
		if (owner === id) { owner = null; written.clear(); }
	}
	function onStorage(event: StorageEvent): void {
		if (event.storageArea && event.storageArea !== storage()) return;
		if (event.key === SIGN_OUT_KEY) {
			if (event.newValue) { release(event.newValue); signedOut(event.newValue); }
			return;
		}
		if (!event.key?.startsWith(DRAFT_STORAGE_PREFIX)) return;
		if (event.newValue === null) sizes.delete(event.key); else sizes.set(event.key, event.key.length + event.newValue.length);
		const mine = owner ? `${DRAFT_STORAGE_PREFIX}${owner}:` : null;
		if (!mine || !event.key.startsWith(mine)) return;
		const key = event.key.slice(mine.length), draft = parseStoredDraft(event.newValue);
		if (event.newValue === null) written.delete(key); else written.set(key, event.newValue);
		changed(key, draft);
	}
	function listen(on: boolean): void {
		if (listening === on || typeof window === 'undefined') return;
		listening = on;
		if (on) window.addEventListener('storage', onStorage);
		else window.removeEventListener('storage', onStorage);
	}

	return {
		/** The draft under `key` changed in memory: it is written now. */
		save: write,
		/** The draft left memory (posted, cancelled, emptied): it leaves storage now. */
		remove,
		/**
		 * `id` signed in: from now on drafts are kept under that id. Returns that person's stored drafts by key. What another
		 * person kept on this device is never read into memory.
		 */
		signIn(id: string): Map<string, StoredDraft> {
			owner = id; written.clear(); sizes.clear();
			listen(true);
			const found = new Map<string, StoredDraft>(), store = storage();
			if (!store) return found;
			const mine = `${DRAFT_STORAGE_PREFIX}${id}:`, unreadable: string[] = [];
			for (let index = 0; index < store.length; index += 1) {
				const name = store.key(index), value = name?.startsWith(DRAFT_STORAGE_PREFIX) ? store.getItem(name) : null;
				if (!name || value === null) continue;
				const draft = parseStoredDraft(value);
				// An entry no version of this store can read never comes back to anyone, so it does not keep its room.
				if (!draft) { unreadable.push(name); continue; }
				sizes.set(name, name.length + value.length);
				if (name.startsWith(mine)) { found.set(name.slice(mine.length), draft); written.set(name.slice(mine.length), value); }
			}
			for (const name of unreadable) try { store.removeItem(name); } catch { /* storage refused */ }
			return found;
		},
		/** `id` signed out: every draft kept under that id goes, and nothing more is written until someone signs in. */
		signOut(id: string): void {
			const store = storage(), mine = `${DRAFT_STORAGE_PREFIX}${id}:`;
			if (store) {
				const names: string[] = [];
				for (let index = 0; index < store.length; index += 1) { const name = store.key(index); if (name?.startsWith(mine)) names.push(name); }
				for (const name of names) { try { store.removeItem(name); } catch { /* storage refused */ } sizes.delete(name); }
				// Every other window hears the set, and nothing of it stays stored.
				try { store.setItem(SIGN_OUT_KEY, id); store.removeItem(SIGN_OUT_KEY); } catch { /* storage refused */ }
			}
			release(id);
		},
		/** Tests only: a fresh page. */
		reset(): void {
			owner = null; written.clear(); sizes.clear();
			listen(false);
		},
	};
}
