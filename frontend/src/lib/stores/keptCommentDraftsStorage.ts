// Where `keptCommentDrafts` keeps a draft on this device, so it outlives a reload, a quit and an update restart: one
// browser storage entry per draft, under the signed-in person's id. Only `keptCommentDrafts` calls this, so a draft has
// one writer. Stored: the text, its tagged agents and its one client id; never the caret or why a post failed.
import type { MentionOccurrence } from '$lib/components/comments/mentionSelection';

export type StoredDraft = { body: string; tags: MentionOccurrence[]; clientId?: string };

export const DRAFT_STORAGE_PREFIX = 'corsia:commentDraft:v1:';
/**
 * Storage counts a string in UTF-16 code units, key and value both. Chromium gives an origin 5,241,856 of them,
 * whatever the script (measured). The drafts of every person on this device take at most 2,000,000, so the others
 * using this origin's storage keep the rest; one draft takes at most a tenth of that, so one very long draft (the server
 * sets no limit on a comment's length) still leaves room for nine more as long, and never pushes out one stored already.
 */
export const DRAFTS_MAX_CHARS = 2_000_000;
export const DRAFT_MAX_CHARS = 200_000;
/** How long typing pauses before the draft is written; leaving the page writes at once. */
export const DRAFT_WRITE_DELAY_MS = 300;

/** A stored entry read back, or null when it is not one this version wrote (it is left where it is). */
export function parseStoredDraft(value: string | null): StoredDraft | null {
	if (!value) return null;
	try {
		const parsed = JSON.parse(value) as Partial<StoredDraft>;
		const body = parsed?.body, tags = parsed?.tags, clientId = parsed?.clientId;
		if (typeof body !== 'string' || !body || !Array.isArray(tags)) return null;
		if (clientId !== undefined && typeof clientId !== 'string') return null;
		const valid = tags.every((tag) => tag && typeof tag.agent_id === 'string' && Number.isInteger(tag.start) && Number.isInteger(tag.end)
			&& tag.start >= 0 && tag.start < tag.end && tag.end <= body.length);
		return valid ? { body, tags: tags.map(({ agent_id, start, end }) => ({ agent_id, start, end })), ...(clientId ? { clientId } : {}) } : null;
	} catch {
		return null;
	}
}

type Options = {
	/** The draft under `key` as it is in memory now, or null when there is nothing to keep. */
	read(key: string): StoredDraft | null;
	/** Another window of this origin changed or removed the signed-in person's draft under `key`. */
	changed(key: string, draft: StoredDraft | null): void;
};

export function createDraftStorage({ read, changed }: Options) {
	let owner: string | null = null;
	/** The last value this window wrote or read for each of the owner's drafts, so an unchanged draft is not written again. */
	const written = new Map<string, string>();
	/** The size of every stored draft on this device, every person's, so the cap is checked without reading them back. */
	const sizes = new Map<string, number>();
	const pending = new Set<string>();
	const warned = new Set<string>();
	let timer: ReturnType<typeof setTimeout> | undefined;
	let listening = false;

	const storage = (): Storage | null => {
		try { return typeof localStorage === 'undefined' ? null : localStorage; } catch { return null; }
	};
	const entry = (key: string) => `${DRAFT_STORAGE_PREFIX}${owner}:${key}`;
	const total = () => { let sum = 0; for (const size of sizes.values()) sum += size; return sum; };
	function warn(key: string, chars: number, reason: string): void {
		if (warned.has(key)) return;
		warned.add(key);
		console.warn(`[drafts] An unsent comment (${chars} characters) is kept in this window only: ${reason}. It stays until it posts or is cancelled.`);
	}
	function write(key: string): void {
		const store = storage();
		if (!store || !owner) return;
		const draft = read(key), name = entry(key);
		if (!draft) { remove(key); return; }
		const value = JSON.stringify(draft);
		if (written.get(key) === value) return;
		const size = name.length + value.length;
		// Over the cap, the version stored last stays as it was: nothing already kept is lost to make room.
		if (size > DRAFT_MAX_CHARS) return warn(key, value.length, 'it is longer than one draft may be on this device');
		if (total() - (sizes.get(name) ?? 0) + size > DRAFTS_MAX_CHARS) return warn(key, value.length, 'the drafts kept on this device are full');
		try {
			store.setItem(name, value);
		} catch {
			return warn(key, value.length, "this device's storage is full");
		}
		written.set(key, value); sizes.set(name, size); warned.delete(key);
	}
	function remove(key: string): void {
		pending.delete(key); warned.delete(key);
		const store = storage();
		if (!store || !owner) return;
		const name = entry(key);
		try { store.removeItem(name); } catch { /* storage refused: nothing more this window can do */ }
		written.delete(key); sizes.delete(name);
	}
	function flush(): void {
		clearTimeout(timer); timer = undefined;
		if (!owner) return;
		const keys = [...pending]; pending.clear();
		for (const key of keys) write(key);
	}
	function onStorage(event: StorageEvent): void {
		if ((event.storageArea && event.storageArea !== storage()) || !event.key?.startsWith(DRAFT_STORAGE_PREFIX)) return;
		if (event.newValue === null) sizes.delete(event.key); else sizes.set(event.key, event.key.length + event.newValue.length);
		const mine = owner ? `${DRAFT_STORAGE_PREFIX}${owner}:` : null;
		if (!mine || !event.key.startsWith(mine)) return;
		const key = event.key.slice(mine.length), draft = parseStoredDraft(event.newValue);
		if (event.newValue === null) written.delete(key); else written.set(key, event.newValue);
		pending.delete(key);
		changed(key, draft);
	}
	function onHidden(): void { if (document.visibilityState === 'hidden') flush(); }
	function listen(on: boolean): void {
		if (listening === on || typeof window === 'undefined') return;
		listening = on;
		if (on) {
			window.addEventListener('pagehide', flush);
			document.addEventListener('visibilitychange', onHidden);
			window.addEventListener('storage', onStorage);
		} else {
			window.removeEventListener('pagehide', flush);
			document.removeEventListener('visibilitychange', onHidden);
			window.removeEventListener('storage', onStorage);
		}
	}

	return {
		/** The draft under `key` changed in memory: it is written once typing pauses, or when the page is left. */
		schedule(key: string): void {
			pending.add(key);
			clearTimeout(timer);
			timer = setTimeout(flush, DRAFT_WRITE_DELAY_MS);
		},
		/** The draft left memory (posted, cancelled, emptied): it leaves storage now, and a write still waiting goes with it. */
		remove,
		flush,
		/**
		 * `id` signed in: from now on drafts are kept under that id. Returns that person's stored drafts by key. What another
		 * person kept on this device is never read into memory.
		 */
		signIn(id: string): Map<string, StoredDraft> {
			flush();
			owner = id; written.clear(); pending.clear(); warned.clear(); sizes.clear();
			listen(true);
			const found = new Map<string, StoredDraft>(), store = storage();
			if (!store) return found;
			const mine = `${DRAFT_STORAGE_PREFIX}${id}:`;
			for (let index = 0; index < store.length; index += 1) {
				const name = store.key(index), value = name?.startsWith(DRAFT_STORAGE_PREFIX) ? store.getItem(name) : null;
				if (!name || value === null) continue;
				sizes.set(name, name.length + value.length);
				const draft = name.startsWith(mine) ? parseStoredDraft(value) : null;
				if (draft) { found.set(name.slice(mine.length), draft); written.set(name.slice(mine.length), value); }
			}
			return found;
		},
		/** `id` signed out: every draft kept under that id goes, and nothing more is written until someone signs in. */
		signOut(id: string): void {
			clearTimeout(timer); timer = undefined; pending.clear(); warned.clear();
			const store = storage(), mine = `${DRAFT_STORAGE_PREFIX}${id}:`;
			if (store) {
				const names: string[] = [];
				for (let index = 0; index < store.length; index += 1) { const name = store.key(index); if (name?.startsWith(mine)) names.push(name); }
				for (const name of names) { try { store.removeItem(name); } catch { /* storage refused */ } sizes.delete(name); }
			}
			if (owner === id) { owner = null; written.clear(); }
		},
		/** Tests only: a fresh page. */
		reset(): void {
			clearTimeout(timer); timer = undefined;
			owner = null; written.clear(); sizes.clear(); pending.clear(); warned.clear();
			listen(false);
		},
	};
}
