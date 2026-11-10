/** One email of the conversation a draft replies into. Read-only: it is never part of the draft's text. */
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

/** A file the draft sends, as the draft record pins it. */
export interface EmailAttachment {
	name: string;
	path: string;
	size: number;
	type: 'image' | 'document';
	/** The pinned file is gone: it was moved or deleted after the draft saved. */
	unavailable?: boolean;
}

/**
 * What the canvas shows around an email draft, as `GET /api/email-drafts/{artifact_id}/context` returns it. An added
 * recipient, a bad address and newer emails are not stored anywhere: each is read here from the draft's own To and Cc
 * and from the conversation.
 */
export interface EmailDraftContext {
	/** Context the person may want, shown folded into one line under the header. */
	notes: string[];
	/** The id of the email the draft answers. Emails above it in `conversation` came after it. Absent: a new email. */
	repliedTo?: string;
	/** Every address on the email being answered, its sender's included, lowercase. Absent when it couldn't be read. */
	answered?: string[];
	/** The files the send will attach, read from the draft record. */
	attachments?: EmailAttachment[];
	/** Newest first: the emails that came after the one being replied to, then that one, then the older ones. */
	conversation: EmailMessage[];
	status: ConversationStatus;
	/** With `error`: the draft's account can't be used here, or the account answered but the read failed. */
	reason?: 'account' | 'unreadable';
	/** With `partial`: how many of the conversation's emails could not be read. */
	unreadable?: number;
}

// The backend's plain address (connections/mail/email_document.py `_ADDRESS`): the send refuses anything else.
const PLAIN_ADDRESS = /^[^\s,;<>"()[\]\\@]+@[^\s,;<>"()[\]\\@]+$/;
const CONTROL = /[\p{Cc}\p{Cf}]/u;

/** Where the email the draft answers sits in the conversation (newest first); -1 when it isn't there. */
function repliedToIndex(context: Pick<EmailDraftContext, 'conversation' | 'repliedTo'>): number {
	return context.repliedTo ? context.conversation.findIndex((message) => message.id === context.repliedTo) : -1;
}

/** The emails that came after the one the draft answers: the ones above it in the conversation. */
export function newerEmails(context: Pick<EmailDraftContext, 'conversation' | 'repliedTo'>): EmailMessage[] {
	return context.conversation.slice(0, Math.max(0, repliedToIndex(context)));
}

/** The id of the email the draft answers when the conversation shows it, else null. */
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

/** Whether the send takes this entry as an address: the backend's plain-address rule, read the same way. */
export function isFullAddress(entry: string): boolean {
	return PLAIN_ADDRESS.test(entry) && !CONTROL.test(entry);
}

/** The entries in To or Cc the send would refuse, as typed. */
export function badEntries(text: string): string[] {
	return recipientEntries(text).filter((entry) => !isFullAddress(entry));
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
