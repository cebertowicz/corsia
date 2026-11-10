import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import type { ConversationFailure, EmailDraftContext } from '$lib/emailDraft';

/** The server's answer for one draft (backend/api/email_drafts.py), before the view's own names. */
interface ContextResponse {
	status: 'ready' | 'error';
	reason: 'account' | 'unreadable' | null;
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
	const failed = (reason: ConversationFailure): EmailDraftContext => ({ status: 'error', reason, conversation: [], notes: [], attachments: [] });
	let body: ContextResponse;
	try {
		const res = await fetch(`/api/email-drafts/${encodeURIComponent(artifactId)}/context`, {
			headers: { Authorization: `Bearer ${get(token) ?? ''}` },
		});
		if (!res.ok) return failed(REFUSED[res.status] ?? 'unreadable');
		body = (await res.json()) as ContextResponse;
	} catch {
		return failed('unreadable');
	}
	return {
		status: body.status, reason: body.reason ?? undefined, repliedTo: body.replied_to ?? undefined,
		answered: body.answered ?? undefined, conversation: body.conversation ?? [],
		attachments: body.attachments ?? [], notes: body.notes ?? [],
	};
}
