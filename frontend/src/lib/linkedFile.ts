// Which workspace file a rendered link names. The server turns a message's text into
// reference cards with a Python port of this path logic (backend/registry/path_cards.py).

import { RequestRefused, type DocRevision, type ScopeRoot } from '$lib/api/docs';
import { withoutLineSuffix } from '$lib/cm/markdown-link';

export function targetPath(href: string): string | null {
	const withoutQuery = href.split(/[?#]/, 1)[0];
	if (!withoutQuery) return null;
	try {
		return withoutLineSuffix(decodeURIComponent(withoutQuery));
	} catch {
		return null;
	}
}

function normalizePath(root: string, target: string): string | null {
	const rootParts = root.split('/').filter(Boolean);
	const parts = target.startsWith('/') ? [] : [...rootParts];
	for (const part of target.split('/')) {
		if (!part || part === '.') continue;
		if (part === '..') {
			if (parts.length <= rootParts.length) return null;
			parts.pop();
			continue;
		}
		parts.push(part);
	}
	const resolved = `/${parts.join('/')}`;
	return resolved === root || resolved.startsWith(`${root}/`) ? resolved : null;
}

function workspacePath(href: string, roots: ScopeRoot[]): string | null {
	const target = targetPath(href);
	if (!target) return null;
	const workspaces = roots.filter((root) => root.kind === 'workspace');
	if (target.startsWith('/')) {
		const root = workspaces.find((candidate) => target === candidate.path || target.startsWith(`${candidate.path}/`));
		return root ? normalizePath(root.path, target) : null;
	}
	return workspaces[0] ? normalizePath(workspaces[0].path, target) : null;
}

export interface LinkedFileReads {
	fetchRoots: (projectId: string | null) => Promise<ScopeRoot[]>;
	fetchRevision: (path: string, projectId: string | null) => Promise<DocRevision>;
}

/**
 * The linked file and its revision, resolved from the scope's root first. In a project,
 * a relative link with no file there is tried once from the workspace root, through the
 * same revision route and its scope check; its scope refusal means the same as no file.
 * Any other failure of that read is a failed read, not a missing file, and is thrown.
 */
export async function linkedFile(href: string, reads: LinkedFileReads, projectId: string | null): Promise<{ path: string; revision: DocRevision } | null> {
	const own = workspacePath(href, await reads.fetchRoots(projectId));
	const ownRevision = own ? await reads.fetchRevision(own, projectId) : null;
	if (own && ownRevision?.exists) return { path: own, revision: ownRevision };
	if (!projectId || targetPath(href)?.startsWith('/')) return null;
	const other = workspacePath(href, await reads.fetchRoots(null));
	if (!other || other === own) return null;
	try {
		const revision = await reads.fetchRevision(other, projectId);
		return revision.exists ? { path: other, revision } : null;
	} catch (error) {
		if (isScopeRefusal(error)) return null;
		throw error;
	}
}

// The revision route's only 400 is its scope refusal (backend/api/files_revision.py).
function isScopeRefusal(error: unknown): boolean {
	return error instanceof RequestRefused && error.message.startsWith('400 ');
}
