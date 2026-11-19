import { textSha256 } from './documentWire';

/** `detail.code` of the 409 the editor save answers when the file changed after the tab's base (issue #3049). */
export const EDITOR_STALE_CODE = 'stale_base';

/**
 * The digest a non-live save sends as `expected_sha256`: the SHA-256 of the text the tab last
 * saved or loaded, which is the file's bytes when this tab started editing. Null sends the
 * plain overwrite this endpoint always did, and is the answer whenever the digest cannot be
 * trusted or computed — a save is never held up by it.
 *
 * - A baseline holding U+FFFD may have been decoded lossily from bytes that were not valid
 *   UTF-8 (the open replaces them), so its digest can never match the file; that file keeps
 *   the old overwrite rather than refusing every save for ever.
 * - A hashing failure sends no digest and the save goes ahead.
 */
export async function baseDigest(base: string): Promise<string | null> {
	if (base.includes('�')) return null;
	try {
		return await textSha256(base);
	} catch (error) {
		console.error('doc save base digest failed; saving without it', error);
		return null;
	}
}

/** Remembers each tab's last digest so typing, which saves every pause, hashes a document once per base. */
export function createBaseDigests() {
	const byKey = new Map<string, { base: string; digest: Promise<string | null> }>();
	return {
		for(key: string, base: string): Promise<string | null> {
			const known = byKey.get(key);
			if (known && known.base === base) return known.digest;
			const digest = baseDigest(base);
			byKey.set(key, { base, digest });
			return digest;
		},
		clear(key: string): void { byKey.delete(key); },
	};
}
