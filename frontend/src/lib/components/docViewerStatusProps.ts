import type { Snippet } from 'svelte';
import type { DocOwnership } from '$lib/api/docOwnership';
import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
import type { DocOpenRefusal, LiveUnavailable } from '$lib/docOpenRefusal';
import type { DocumentFreshness } from '$lib/docFreshness';
import type { DocumentAsk } from '$lib/stores/documentBar';

export interface DocViewerStatusProps {
	presence?: Snippet;
	touchReading?: boolean;
	editing?: boolean;
	canEdit?: boolean;
	onEnterEdit?: () => void;
	onDone?: () => void;
	/** The ask's card, docked to the canvas's bottom edge; its buttons come before the document in tab order. */
	reviewBar?: Snippet;
	ask?: DocumentAsk | null;
	commentsOpen?: boolean;
	commentsLoading?: boolean;
	saveStatus?: 'idle' | 'saving' | 'saved';
	ready: boolean;
	stale: boolean;
	readonly: boolean;
	commentCount: number;
	unreadCount?: number;
	artifactId: string | null;
	targetInstanceId: string | null;
	viewingThreadId?: string | null;
	reviewTargetKey?: string | null;
	onReload?: () => void;
	onOpenComments: (opener: HTMLElement) => void;
	freshness: DocumentFreshness | null;
	/** Who owns and supervises a registered document; its row shows both, each with its own picker. */
	ownership?: DocOwnership | null;
	/** The row is the one this tab last showed, drawn before the load answered: its owner pickers and Mark verified wait for the load. */
	headerStandIn?: boolean;
	onOwnershipChanged?: (value: DocOwnership) => void;
	onFeedbackChanged?: () => void | Promise<void>;
	onMarkVerified?: () => Promise<void>;
	liveSession?: LiveDocumentSession | null;
	openRefusal?: DocOpenRefusal | null;
	liveUnavailable?: LiveUnavailable | null;
	tabText?: string;
	onReopen?: () => void;
	onRead?: () => void;
	/** An image of this document covers it; its viewer owns the image lane's Send until it is left. */
	imageOpen?: boolean;
	/** The document is not on screen (an image covers it, or it is still being put back): the bar draws none of its controls, as none of it takes input. */
	offScreen?: boolean;
	activityRoot?: HTMLElement | null;
	/** The file has recorded versions (slice 1: instruction files). Without them there is no History, and no count. */
	historyAvailable?: boolean;
	historyOpen?: boolean;
	onOpenHistory?: () => void;
	/** The Comments button wherever the bar draws it: the list the canvas opens on its own hangs from it. */
	commentsEntry?: HTMLButtonElement | null;
	/** The Details button: what a list or picker opened from inside the closed panel returns to. */
	detailsEntry?: HTMLButtonElement | null;
}
