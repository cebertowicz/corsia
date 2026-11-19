import type { DocumentFreshness } from '$lib/docFreshness';
import type { DocOwnership } from '$lib/api/docOwnership';
import type { EmailDraftInfo } from '$lib/emailDraft';

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
	ownership?: DocOwnership | null;
	/** The file's versions are recorded, so it has a History. */
	history?: boolean;
	/** On a live_open read: false with a reason when the document opens as a non-live file. */
	live?: boolean;
	live_unavailable?: string;
	/** Set when the file's draft record says it is an email draft: the record's id and the header as the server read it. */
	email_draft?: EmailDraftInfo | null;
	/** Set when the file holds agent text newer than its last fresh read: the text served is the passed version. */
	passed_version?: PassedVersion | null;
}

/** The version a fresh read passed, shown in place of newer agent text the reader has not passed yet. */
export interface PassedVersion {
	sha256: string;
	fresh_read_id: string | null;
	current_sha256: string;
	/** The line the viewer shows the person about why they see this version. */
	notice: string;
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
