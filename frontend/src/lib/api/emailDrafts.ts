import type { ConversationFailure, EmailDraftContext, EmailDraftState } from '$lib/emailDraft';
import { apiFetch } from './request';
/** The server's answer for one draft (backend/api/email_drafts.py), before the view's own names. */
interface ContextResponse {
	state: EmailDraftState;
	read_at: string | null;
	status: 'ready' | 'error';
	reason: 'account' | 'unreadable' | null;
	/** With `unreadable`: the reference the server logged the failure under. */
	reference?: string;
	/** With `account`: the account's own fixed refusal, naming the cause. */
	detail?: string;
	/** The chat the draft belongs to. */
	thread_id?: string;
	replied_to: string | null;
	answered: string[] | null;
	conversation: EmailDraftContext['conversation'];
	attachments: EmailDraftContext['attachments'];
	notes: string[];
}

/** The refusals the route names: the person's CORSIA sign-in ended, or the draft isn't one of theirs. */
const REFUSED: Record<number, ConversationFailure> = { 401: 'signin', 404: 'missing' };

/**
 * What the draft view shows around an email draft, read from the draft's own mail account each time it opens. A
 * request that fails before the server answers is the same named state as a failed read, so the view offers its retry.
 */
export async function fetchEmailDraftContext(artifactId: string): Promise<EmailDraftContext> {
	const failed = (reason: ConversationFailure, reference?: string): EmailDraftContext => ({ status: 'error', reason, reference, conversation: [], notes: [], attachments: [] });
	let body: ContextResponse;
	try {
		const res = await apiFetch(`/api/email-drafts/${encodeURIComponent(artifactId)}/context`);
		// A server error answers with the reference it logged (backend/main.py).
		if (!res.ok) return failed(REFUSED[res.status] ?? 'unreadable', (await res.json().catch(() => null))?.reference ?? undefined);
		body = (await res.json()) as ContextResponse;
	} catch {
		return failed('unreadable');
	}
	return {
		state: body.state, readAt: body.read_at ?? null, status: body.status, reason: body.reason ?? undefined, reference: body.reference, detail: body.detail, threadId: body.thread_id, repliedTo: body.replied_to ?? undefined,
		answered: body.answered ?? undefined, conversation: body.conversation ?? [],
		attachments: body.attachments ?? [], notes: body.notes ?? [],
	};
}

export type DraftActionResult =
	| { state: 'sent'; not_delivered_to?: string[] }
	| { state: 'discarded' | 'sending' | 'unconfirmed' | 'failed'; reason?: string }
	| { state: 'changed' | 'newer'; message: string }
	| { state: 'signin' | 'missing' }
	/** The server didn't answer the action (an error status): whether it happened is read from the draft's state. */
	| { state: 'unknown' };

async function draftAction(artifactId: string, action: 'send' | 'discard', body?: { text: string; read_at: string | null }): Promise<DraftActionResult> {
	const res = await apiFetch(`/api/email-drafts/${encodeURIComponent(artifactId)}/${action}`, {
		method: 'POST', ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
	});
	if (res.status === 401) return { state: 'signin' };
	if (res.status === 404) return { state: 'missing' };
	if (res.status === 409) return (await res.json()).detail;
	// Only the server's own 'failed' answer means the email provably didn't leave; an error status says nothing either way.
	if (!res.ok) return { state: 'unknown' };
	return await res.json() as DraftActionResult;
}

export function sendEmailDraft(artifactId: string, text: string, readAt: string | null): Promise<DraftActionResult> {
	return draftAction(artifactId, 'send', { text, read_at: readAt });
}

/** The draft's state as the server holds it, or null when the read didn't reach the server. */
export async function readEmailDraftState(artifactId: string): Promise<EmailDraftState | null> {
	return (await fetchEmailDraftContext(artifactId)).state ?? null;
}

export function discardEmailDraft(artifactId: string): Promise<DraftActionResult> {
	return draftAction(artifactId, 'discard');
}
