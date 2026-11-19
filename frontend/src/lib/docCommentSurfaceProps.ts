import type { DocComment, CommentLoad } from '$lib/stores/docComments';
import type { MentionOccurrence, createMentionSelection } from '$lib/components/comments/mentionSelection';
import type { WidgetLabelReader } from '$lib/docCommentLabels';
import type { ImageCommentSurface, DocImageComments } from '$lib/docImageComments';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { CommentAnchor, CommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';
import type { DocFeedback } from '$lib/docFeedback.svelte';

/** What DocCommentSurface takes; `resolvedCount` and `resolvedOpen` are bindable. */
export interface DocCommentSurfaceProps {
	state: CommentSurfaceState;
	onThreadMissing?: (remaining: string[]) => void;
	/** The column opened on its own at load, so opening it does not hide the sidebar or the Workspace panel. */
	quiet?: boolean;
	anchor: CommentAnchor | null;
	/** The anchor is where a click opened the card: the card opens just under it, not beside the text. */
	anchorAtClick?: boolean;
	/** The canvas the card stays inside. */
	bounds?: HTMLElement | null;
	/** The pane is a drawer over the canvas: the margin column has no room. */
	narrow: boolean;
	/** A phone: a card or the composer is the sheet. Anywhere else with no margin they are a popover at the click; a surface that does not say follows `narrow`. */
	phone?: boolean | null;
	/** With the margin column up, lets the picked card go; answers whether one was picked. */
	onRelease?: () => boolean;
	comments: DocComment[];
	/** Reads a table comment's label from the editor's own syntax tree. */
	widgetLabel?: WidgetLabelReader;
	focusedId: string | null;
	focusedIds?: string[] | null;
	/** The thread whose text is under the pointer: its card wears the focused ring and nothing more; only the pick (`focusedId`) opens, places or reads a card. */
	ringId?: string | null;
	/** `label` names what a non-text comment is on, in place of the quote. */
	draft: { text: string; body?: string; label?: string; mentionSelection?: ReturnType<typeof createMentionSelection> } | null;
	draftReason?: string;
	onDraftBody?: (text: string) => void;
	onCancelDraft?: () => void;
	onClose: () => void;
	restoreFocus: () => void;
	onFocus: (id: string, placement?: 'top') => void;
	onSubmit: (text: string, taggedAgentIds: string[], mentions: MentionOccurrence[]) => Promise<void>;
	onReply: (id: string, text: string, taggedAgentIds: string[], mentions: MentionOccurrence[]) => Promise<void>;
	/** With `wait` the comment changes only once the request is answered; the promise says whether it was taken. */
	onResolve: (id: string, resolved: boolean, wait?: boolean) => void | Promise<boolean>;
	onAgree: (id: string, agreed: boolean) => void;
	onViewed?: (id: string, replyId: string) => Promise<void>;
	target: DocViewerTarget | null;
	/** The commented document's path for comment links, used only when there is no `target` (the image lightbox). */
	docPath?: string | null;
	artifactId: string | null;
	/** The document's long-lived feedback controller, shared with the drawer's delivery controls. */
	feedback?: DocFeedback;
	readOnly?: boolean;
	/** A decision is in flight (`busy`): the editor holds still, and while it is an Accept the suggestion `id` is marked busy. */
	onSuggestionBusy?: (busy: boolean, id?: string) => void;
	onSuggestionAccepted?: (text: string) => void;
	/** The server applied an accepted edit but could not record the decision: read the shown document again. */
	onDocumentChanged?: () => void;
	load?: CommentLoad;
	onRetry?: () => void;
	image?: ImageCommentSurface | null;
	images?: DocImageComments | null;
	/** An image opened from the document covers it, the drawer with it: the drawer stays open beneath, and gets its focus back after. */
	covered?: boolean;
	/** The document is not on screen (an image covers it, or it is still being put back): nothing is marked read. */
	offScreen?: boolean;
	/** Whether the comment list's drawer is on screen, in either form: true from its mount until its outro has ended. */
	onDrawerShown?: (shown: boolean) => void;
	/** The sheet controller's modal presence, including its closing scrim. */
	onSheetModalChange?: (open: boolean) => void;
	/** Asks the editor to keep the draft's text, or the text a thread sheet was opened from, clear of the sheet, now and whenever it moves. */
	onRevealDraft?: (opening: boolean) => void; onRevealCard?: (opening: boolean, panel: HTMLElement | null) => void;
	/** Resolved threads at every width (zero on an image's drawer), for the header control that opens their drawer. */
	resolvedCount?: number;
	/** A new comment is being written over the margin column: the column stays, its cards where they were, with the composer beside the text. */
	draftOverColumn?: boolean;
	/** Changes each time the reader opens something (the surface state's count of picks, drafts and lists). */
	opened?: number;
	/** The resolved drawer is open: set by that control only. */
	resolvedOpen?: boolean;
	/** The document's Comments button, drawn in the app bar or tab strip outside the canvas: a list the canvas opened hangs from it. */
	commentsEntry?: HTMLElement | null;
}
