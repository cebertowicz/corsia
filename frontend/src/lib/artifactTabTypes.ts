export type ArtifactTabStatus = 'ready' | 'loading' | 'error' | 'unavailable' | 'readonly';

/** In-session camera position for an image viewport; null scale keeps it fitted to the viewport. */
export interface ImageViewportPlace {
	scrollLeft: number;
	scrollTop: number;
	scale: number | null;
}

export interface ArtifactTab {
	/** The artifact leaf hosts documents, child conversations, and bounded legacy discussion archives. */
	kind: 'document' | 'subagent' | 'legacy-discussion' | 'browser';
	/** Immutable tab-instance key. Never derived from artifact identity. */
	instanceId: string;
	/** Primary artifact identity once resolved. */
	artifactId: string | null;
	/** Current physical path — fallback identity, and the location to load. */
	path: string;
	title: string;
	/** Child thread identity for a subagent conversation leaf. */
	threadId: string | null;
	/** Plan node whose trailing control opened this leaf; absent on older persisted tabs. */
	sourceNodeId?: string | null;
	/** When that node is a step of a nested subagent plan: the step of the thread's own plan that nests it. */
	sourceStepId?: string | null;
	/** Revision this tab last successfully loaded — the basis for the changed dot. */
	loadedRevision: string | null;
	/** Latest revision observed on the server; null until this session verifies it. */
	latestRevision: string | null;
	status: ArtifactTabStatus;
}

export interface ThreadArtifactWorkspace {
	tabs: ArtifactTab[];
	activeTabId: string | null;
	visible: boolean;
	activeSurface: 'chat' | 'artifact';
}

export interface OpenArtifactInput {
	artifactId?: string | null;
	path: string;
	title: string;
}

export interface OpenSubagentInput {
	threadId: string;
	title: string;
	/** Absent for an entry point with no plan node — the chip, a message
	 * attribution, or a send reference (#1949). Only a plan-node opener sets it. */
	sourceNodeId?: string;
	/** Set with `sourceNodeId` when that node belongs to a nested subagent plan: the owning step of this thread's plan. */
	sourceStepId?: string;
}

export interface OpenBrowserInput {
	threadId: string;
	title?: string;
}

export interface OpenLegacyDiscussionInput {
	artifactId: string;
	title: string;
}

/**
 * Result of one load attempt, always carrying the identity that initiated it.
 *
 * Async load callbacks must never be applied to "whichever tab is active now":
 * a slow load in tab A that settles after the user switched to tab B would
 * otherwise stamp A's status, revision, and resolved id onto B.
 */
export interface TabLoadSignal {
	/** Instance key of the tab that started this load. */
	instanceId: string;
	status: 'loading' | 'ready' | 'error' | 'unavailable';
	/** Server-resolved artifact id, when this load resolved one. */
	artifactId?: string | null;
	/** Server content revision for a successful load. */
	revision?: string | null;
	/** False when the doc API would refuse a write to this path. */
	writable?: boolean;
	/**
	 * Which viewer the backend says this file needs. Viewer-local and never stored on the tab: the
	 * leaf reads it, the workspace store ignores it, and each load decides it again.
	 */
	renderer?: 'image' | 'document';
}

export interface RevisionCheck {
	exists: boolean;
	/** Authoritative current path — differs from the tab's when the doc relocated. */
	path: string | null;
	revision: string | null;
	writable: boolean;
	artifactId: string | null;
}

/** The workspace store may defer a merge while either instance owns an active draft. */
export type DuplicateMergeGuard = (survivorInstanceId: string, duplicateInstanceId: string) => boolean;
