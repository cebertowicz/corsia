/** One email of the conversation a draft replies into. Read-only: it is never part of the draft's text. */
export interface EmailMessage {
	id: string;
	/** The From header as the mail host gave it, e.g. "Dana Whitfield <dana.whitfield@example.com>". */
	sender: string;
	/** Server timestamp, read by `time.ts`. */
	date: string;
	body: string;
}

/** Where the conversation read stands. */
export type ConversationStatus = 'ready' | 'loading' | 'error';

/**
 * Why the conversation couldn't be read: the draft's account is signed out or removed, the account answered but the read
 * failed, the person's CORSIA sign-in ended, or the draft can't be found for them.
 */
export type ConversationFailure = 'account' | 'unreadable' | 'signin' | 'missing';

/** A file the draft sends, as the draft record pins it. */
export interface EmailAttachment {
	name: string;
	path: string;
	size: number;
	type: 'image' | 'document';
	/** The pinned file is gone: it was moved or deleted after the draft saved. */
	unavailable?: boolean;
}

/** One To or Cc entry as the server read the draft's header: its range in the document and whether the send takes it. */
export interface HeaderRecipient {
	field: 'to' | 'cc';
	text: string;
	/** UTF-16 offsets into the document, as the editor counts them. */
	from: number;
	to: number;
	/** The send takes it as an address (connections/mail/email_document.py `is_plain_address`). */
	full: boolean;
}

/** The draft's header as the server read it (`read_header`): its text, and each To and Cc entry. */
export interface ServerHeader {
	text: string;
	recipients: HeaderRecipient[];
}

/** What a document load or live frame says about an email draft: its record's id and its header as the server read it. */
export interface EmailDraftInfo {
	id: string;
	header: ServerHeader;
}

/**
 * What the canvas shows around an email draft, as `GET /api/email-drafts/{artifact_id}/context` returns it. Newer emails
 * are read here from the conversation; an added recipient and a bad address from the server's reading of the header.
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
	/** With `error`: why the read failed. */
	reason?: ConversationFailure;
}

/** The labels the header view knows; any other line is still a header line, drawn as one being typed. */
const HEADER_FIELD = /^(from|to|cc|subject)\s*:/i;

/** One header line as the view lays it out: the field its label names ('' for none) and its document range. */
export interface HeaderLine {
	key: string;
	from: number;
	to: number;
	/** Where the value starts, after the label. */
	valueFrom: number;
}

/**
 * The draft's header as the view lays it out: every line up to the first blank line, as the server splits it, each with
 * the field its label names. Layout only: which entries are addresses the send takes is the server's reading.
 */
export function headerLayout(lines: Iterable<string>): { text: string; lines: HeaderLine[] } {
	const found: HeaderLine[] = [], texts: string[] = [];
	let at = 0;
	for (const line of lines) {
		if (line === '') break;
		const match = HEADER_FIELD.exec(line);
		found.push({ key: match ? match[1].toLowerCase() : '', from: at, to: at + line.length, valueFrom: at + (match?.[0].length ?? 0) });
		texts.push(line);
		at += line.length + 1;
	}
	return { text: texts.join('\n'), lines: found };
}

/** The server's reading of the recipients while it read this same header text; null while an edit it hasn't read is in. */
export function currentRecipients(header: ServerHeader | null | undefined, headerText: string): HeaderRecipient[] | null {
	return header && header.text === headerText ? header.recipients : null;
}

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

/**
 * The To and Cc entries the send would refuse, as the amber line names them. `recipients` is the server's reading of the
 * header as it reads now (null while the text differs from what the server read). With live editing (`rereads`) a new
 * reading follows each edit, so until it arrives the line keeps `shown`; without it none comes, so the line goes.
 */
export function refusedEntries(shown: string[], recipients: HeaderRecipient[] | null, rereads: boolean): string[] {
	if (recipients) return recipients.filter((entry) => !entry.full).map((entry) => entry.text);
	return rereads ? shown : [];
}

/** The amber line for entries in To or Cc that are not full email addresses, or null when there are none. */
export function notFullAddressLine(entries: readonly string[]): string | null {
	if (!entries.length) return null;
	if (entries.length === 1) return `${entries[0]} isn’t a full email address.`;
	return `${entries.length} entries aren’t full email addresses: ${entries.join(', ')}.`;
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
