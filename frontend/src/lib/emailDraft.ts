import { emailDraftFixture } from './emailDraftFixtures';

/** One earlier email of the conversation a draft replies into. Read-only: it is never part of the draft's text. */
export interface EmailMessage {
	id: string;
	/** The From header as the mail host gave it, e.g. "Dana Whitfield <dana.whitfield@example.com>". */
	sender: string;
	/** Server timestamp, read by `time.ts`. */
	date: string;
	body: string;
}

/** Where the conversation read stands. `partial` shows the emails that could be read and says how many could not. */
export type ConversationStatus = 'ready' | 'loading' | 'error' | 'partial';

/**
 * One note on a draft. `loud` notes are what the person must act on before sending and show above the header; `quiet`
 * ones are context and fold into one line under it. An added recipient, a bad address and newer emails are not notes: each is
 * read from the draft's own To and Cc and from the conversation (`EmailDraftContext.repliedTo` marks the email the draft answers).
 */
export interface EmailNote {
	kind: 'loud' | 'quiet';
	text: string;
}

/** A file the draft sends, as the draft record pins it (`target.attachments`: path, name, size, sha256). */
export interface EmailAttachment {
	name: string;
	path: string;
	size: number;
	type: 'image' | 'document';
	/** The pinned file is gone: it was moved or deleted after the draft saved. */
	unavailable?: boolean;
}

/** A comment on a line of an earlier email: it names the email and quotes the line, like a diagram pin names its diagram. */
export interface EmailComment {
	messageId: string;
	quote: string;
	text: string;
}

/** What the canvas shows around an email draft: its notes beside the header and the conversation below it. */
export interface EmailDraftContext {
	notes: EmailNote[];
	/** The id of the email the draft answers. Emails above it in `conversation` came after it. Absent: the first one. */
	repliedTo?: string;
	/** Entries in To or Cc that the safeguard found are not full email addresses, as typed. */
	badRecipients?: string[];
	/** Addresses on the email being answered besides its sender (its other recipients); an address in To or Cc that is on neither is an added recipient. */
	answered?: string[];
	/** The files the send will attach, read from the draft record. */
	attachments?: EmailAttachment[];
	comments?: EmailComment[];
	/** Newest first: the emails that came after the one being replied to, then that one, then the older ones. */
	conversation: EmailMessage[];
	status: ConversationStatus;
	/** With `partial`: how many of the conversation's emails could not be read. */
	unreadable?: number;
	/** The context the same draft gets once the person retries a failed read. */
	afterRetry?: EmailDraftContext;
	/** Prototype only: how the send ends once the 5 seconds run out. The real result comes from the mail account. */
	sendEnds?: 'sent' | 'failed';
	/** Prototype only: the draft opens read-only, as a tab does when its live session isn't writable. */
	readOnly?: boolean;
	/** Prototype only: a document connection that is down, standing in for the live session going offline. */
	offline?: boolean;
	/** Prototype only: an agent asked the person to review this draft, so the Needs-you list holds one item for it, and Send or Don’t send settles that item. */
	review?: boolean;
}

// `in reply to` stays so drafts saved before that line was dropped still open as email drafts.
const HEADER = /^(from|to|cc|subject|in reply to|note)\s*:/i;
const FIXTURE_KEY = 'corsia:emailDraftFixture';

/** The folder the draft tools write email drafts into; the backend's `registry/presentation.py` reads the same folder to pick the envelope icon. */
const EMAIL_DRAFT_FOLDER = 'email-drafts';

/** Whether a document path is an email draft the draft tools wrote (the only mark a draft carries; there is no artifact type for it). */
export function isEmailDraftPath(path: string | null | undefined): boolean {
	const parts = (path ?? '').split('/');
	return parts.length > 1 && parts[parts.length - 2] === EMAIL_DRAFT_FOLDER;
}

/** An email draft document opens with its header lines, then a blank line (`compose_document` writes exactly this). */
export function looksLikeEmailDraft(text: string): boolean {
	const head = text.replace(/\r\n/g, '\n').split('\n\n', 1)[0].split('\n');
	if (head.length < 3 || !head.every((line) => HEADER.test(line))) return false;
	const keys = new Set(head.map((line) => line.slice(0, line.indexOf(':')).trim().toLowerCase()));
	return keys.has('to') && keys.has('subject');
}

/**
 * The conversation data is backend work that has not happened yet, so the prototype reads it from a named fixture
 * kept in localStorage (`corsia:emailDraftFixture`). With no fixture named, nothing changes for any document.
 */
export function prototypeFixtureName(): string | null {
	try { return typeof localStorage === 'undefined' ? null : localStorage.getItem(FIXTURE_KEY); }
	catch { return null; }
}

export function emailDraftContext(text: string, fixture: string | null = prototypeFixtureName(), attempt = 0): EmailDraftContext | null {
	if (!fixture || !looksLikeEmailDraft(text)) return null;
	const found = emailDraftFixture(fixture);
	if (!found) return null;
	const read = attempt > 0 && found.afterRetry ? found.afterRetry : found;
	return { notes: found.notes, repliedTo: found.repliedTo, badRecipients: found.badRecipients, answered: found.answered, attachments: found.attachments, comments: found.comments, conversation: read.conversation, status: read.status, unreadable: read.unreadable, sendEnds: found.sendEnds, readOnly: found.readOnly, offline: found.offline, review: found.review };
}

/**
 * The addresses that were on the email being answered, lowercase: its sender and its other recipients. Null when there is
 * no such email to compare with (a new email, or a conversation that could not be read).
 */
export function addressesOnAnsweredEmail(context: Pick<EmailDraftContext, 'conversation' | 'answered' | 'repliedTo'>): string[] | null {
	const answered = context.conversation[repliedToIndex(context)];
	if (!answered) return null;
	return [senderAddress(answered.sender), ...(context.answered ?? [])].filter(Boolean).map((address) => address.toLowerCase());
}

/** Where the email the draft answers sits in the conversation (newest first); an unknown or missing id means the first. */
function repliedToIndex(context: Pick<EmailDraftContext, 'conversation' | 'repliedTo'>): number {
	const at = context.repliedTo ? context.conversation.findIndex((message) => message.id === context.repliedTo) : 0;
	return at < 0 ? 0 : at;
}

/** The emails that came after the one the draft answers: the ones above it in the conversation. */
export function newerEmails(context: Pick<EmailDraftContext, 'conversation' | 'repliedTo'>): EmailMessage[] {
	return context.conversation.slice(0, repliedToIndex(context));
}

/** The id of the email the draft answers, or null for a new email. */
export function repliedToId(context: Pick<EmailDraftContext, 'conversation' | 'repliedTo'>): string | null {
	return context.conversation[repliedToIndex(context)]?.id ?? null;
}

/** The one line above the draft for emails that came after the one it answers, or null when none did. */
export function newerEmailsLine(newer: readonly EmailMessage[] | undefined): string | null {
	if (!newer?.length) return null;
	if (newer.length === 1) {
		const day = new Date(newer[0].date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
		return `${senderName(newer[0].sender)} wrote on ${day}, after the email you’re replying to.`;
	}
	const names = [...new Set(newer.map((email) => senderName(email.sender)))];
	return `${newer.length} emails came after the one you’re replying to: ${names.join(', ')}.`;
}

/** The entries of the draft's To and Cc lines as typed, trimmed, without empty ones (the header is every line up to the first blank line). */
export function recipientEntries(text: string): string[] {
	const entries: string[] = [];
	for (const line of text.replace(/\r\n/g, '\n').split('\n')) {
		if (line === '') break;
		const match = /^(to|cc)\s*:(.*)$/i.exec(line);
		if (match) entries.push(...match[2].split(',').map((entry) => entry.trim()).filter(Boolean));
	}
	return entries;
}

/** The safeguard's bad entries that are still in To or Cc: an entry the person has changed or removed is no longer named. */
export function badEntriesIn(text: string, bad: readonly string[] | undefined): string[] {
	const now = new Set(recipientEntries(text));
	return (bad ?? []).filter((entry) => now.has(entry));
}

/** The amber line for entries in To or Cc that are not full email addresses, or null when there are none. */
export function notFullAddressLine(entries: readonly string[]): string | null {
	if (!entries.length) return null;
	if (entries.length === 1) return `${entries[0]} isn’t a full email address.`;
	return `${entries.length} entries aren’t full email addresses: ${entries.join(', ')}`;
}

/** A sender as a person reads it: the name, or the address when no name was given. */
export function senderName(sender: string): string {
	return sender.replace(/\s*<[^>]*>\s*$/, '').trim() || sender;
}

/** The sender's address, or empty when the header carries only a name. */
export function senderAddress(sender: string): string {
	return /<([^>]*)>\s*$/.exec(sender)?.[1].trim() ?? (sender.includes('@') ? sender.trim() : '');
}

/** An email's text on one line, for the collapsed row; the row's own width cuts it. */
export function previewLine(body: string): string {
	return body.replace(/\s+/g, ' ').trim();
}
