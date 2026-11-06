import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { ok } from '$lib/api/docs';
import type { DocViewerTarget } from '$lib/docViewerTarget';

export interface DocVersion {
	id: string;
	author: string;
	savedAt: string;
	reason: string | null;
}

function historyUrl(target: DocViewerTarget, commit?: string): string {
	const query = new URLSearchParams({ token: get(token) ?? '', path: target.path });
	if (target.projectId) query.set('project_id', target.projectId);
	if (target.threadId) query.set('thread_id', target.threadId);
	if (commit) query.set('commit', commit);
	return `/api/files/history?${query}`;
}

/** The newest versions; `truncated` says older ones exist past the server's cap. */
export async function fetchHistory(target: DocViewerTarget): Promise<{ versions: DocVersion[]; truncated: boolean }> {
	return ok(await fetch(historyUrl(target)));
}

export async function fetchVersionText(target: DocViewerTarget, commit: string): Promise<string> {
	const data: { text: string } = await ok(await fetch(historyUrl(target, commit)));
	return data.text;
}

/** Records `version` as the newest version while the file is still at `base`; a refusal carries its `kind`. */
export async function restoreVersion(target: DocViewerTarget, version: string, base: string): Promise<{ version: string | null; sha256: string }> {
	return ok(await fetch(`/api/files/history/restore?${new URLSearchParams({ token: get(token) ?? '' })}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ path: target.path, version, base_sha256: base, project_id: target.projectId ?? null, thread_id: target.threadId ?? null }),
	}));
}
