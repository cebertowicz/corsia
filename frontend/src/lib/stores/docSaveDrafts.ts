import { writable, get } from 'svelte/store';
import type { DocViewerTarget } from '$lib/docViewerTarget';

export type DocSaveStatus = 'idle' | 'dirty' | 'saving' | 'saved' | 'error';

export interface DocSaveDraft {
	target: DocViewerTarget;
	editText: string;
	savedText: string;
	status: DocSaveStatus;
	error: string;
	/**
	 * The file changed after this tab's base and the person has not yet chosen which text to keep: the tab offers the choice.
	 * Set only by that refusal; a failure for another reason (the network) leaves it as it was, and it ends only by a save or a choice.
	 */
	stale?: boolean;
	/** How the last save failed, for a surface that must act on the cause: the refusal's code, and whether the failure said anything specific. */
	failure?: { code: string | null; generic: boolean };
	/** Revision the server reported for this draft's last successful write. */
	savedRevision: string | null;
}

export type TargetIdentity = Pick<DocViewerTarget, 'instanceId'>;

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

export function getDraft(key: string): DocSaveDraft | undefined {
	return get(drafts)[key];
}
export function patchDraft(key: string, patch: Partial<DocSaveDraft>): void {
	drafts.update((all) => {
		const cur = all[key];
		if (!cur) return all;
		return { ...all, [key]: { ...cur, ...patch } };
	});
}

export function existingDraft(target: TargetIdentity): DocSaveDraft | undefined {
	return getDraft(draftKey(target));
}

/**
 * The tab's target, carrying the artifact identity its draft resolved since it
 * was seeded. The path is always the tab's: it is what the viewer opens.
 */
export function withDraftArtifactId(target: DocViewerTarget): DocViewerTarget {
	const artifactId = existingDraft(target)?.target.artifactId ?? target.artifactId;
	return artifactId === target.artifactId ? target : { ...target, artifactId };
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
