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
		// The rest of the app opens a registered document by its path, or its external id when it has none.
		const path = artifact?.path ?? artifact?.external_id;
		if (!path) return { status: 'missing' };
		return { status: 'found', path, title: artifact.title ?? null };
	} catch {
		return { status: 'failed' };
	}
}
