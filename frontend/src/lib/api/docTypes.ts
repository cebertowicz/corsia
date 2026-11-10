import type { DocumentFreshness } from '$lib/docFreshness';

export interface ArtifactRow {
	id: string;
	description: string | null;
	parent_thread_id: string;
	updated_at: string;
	[k: string]: unknown;
}

export interface DocContent {
	text: string;
	size: number;
	mtime: number;
	/** Opaque content revision — changes exactly when the bytes change. */
	revision: string;
	/** False when the doc API would refuse a write here (system bootstrap files). */
	writable: boolean;
	artifact: ArtifactRow | null;
	freshness: DocumentFreshness | null;
	/** The file's versions are recorded, so it has a History. */
	history?: boolean;
	/** On a live_open read: false with a reason when the document opens as a non-live file. */
	live?: boolean;
	live_unavailable?: string;
	/** How the app names and marks this file, from the backend's one descriptor rule (`icon` 'mail' is an email draft). */
	presentation?: DocPresentation;
}

export interface DocPresentation {
	label: string;
	icon: string;
}

export interface DocRevision {
	exists: boolean;
	/** Authoritative current path — differs when the document relocated. */
	path: string | null;
	revision: string | null;
	/** The image media type the backend serves this file as; null for anything else. */
	media_type?: string | null;
	size: number | null;
	writable: boolean;
	artifact_id: string | null;
	artifact: ArtifactRow | null;
	freshness: DocumentFreshness | null;
}
