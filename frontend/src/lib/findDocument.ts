import { apiFetch } from '$lib/api/request';

export type DocumentLookup =
	| { status: 'found'; path: string; title: string | null }
	| { status: 'missing' }
	| { status: 'failed' };

/** Resolve the artifact itself; pane membership is independent of document access. */
export async function findDocument(artifactId: string): Promise<DocumentLookup> {
	try {
		const response = await apiFetch(`/api/artifacts/${encodeURIComponent(artifactId)}`, { sessionInAddress: true });
		if (response.status === 404) return { status: 'missing' };
		if (!response.ok) return { status: 'failed' };
		const { artifact } = await response.json();
		if (!artifact?.path) return { status: 'missing' };
		return { status: 'found', path: artifact.path, title: artifact.title ?? null };
	} catch {
		return { status: 'failed' };
	}
}
