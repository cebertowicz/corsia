import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import type { ConversationFailure, EmailDraftContext } from '$lib/emailDraft';
import { apiFetch } from './request';
/** The server's answer for one draft (backend/api/email_drafts.py), before the view's own names. */
interface ContextResponse {
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
		const res = await apiFetch(`/api/email-drafts/${encodeURIComponent(artifactId)}/context`, {
			headers: { Authorization: `Bearer ${get(token) ?? ''}` },
		});
		// A server error answers with the reference it logged (backend/main.py).
		if (!res.ok) return failed(REFUSED[res.status] ?? 'unreadable', (await res.json().catch(() => null))?.reference ?? undefined);
		body = (await res.json()) as ContextResponse;
	} catch {
		return failed('unreadable');
	}
	return {
		status: body.status, reason: body.reason ?? undefined, reference: body.reference, detail: body.detail, threadId: body.thread_id, repliedTo: body.replied_to ?? undefined,
		answered: body.answered ?? undefined, conversation: body.conversation ?? [],
		attachments: body.attachments ?? [], notes: body.notes ?? [],
	};
}
