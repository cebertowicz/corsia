import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import type { EmailDraftContext } from '$lib/emailDraft';

/** The server's answer for one draft (backend/api/email_drafts.py), before the view's own names. */
interface ContextResponse {
	status: 'ready' | 'partial' | 'error';
	reason: 'account' | 'unreadable' | null;
	replied_to: string | null;
	answered: string[] | null;
	conversation: EmailDraftContext['conversation'];
	unreadable: number;
	attachments: EmailDraftContext['attachments'];
	notes: string[];
}

/**
 * What the draft view shows around an email draft, read from the draft's own mail account each time it opens. A
 * request that fails before the server answers is the same named state as a failed read, so the view offers its retry.
 */
export async function fetchEmailDraftContext(artifactId: string): Promise<EmailDraftContext> {
	let body: ContextResponse;
	try {
		const res = await fetch(`/api/email-drafts/${encodeURIComponent(artifactId)}/context`, {
			headers: { Authorization: `Bearer ${get(token) ?? ''}` },
		});
		if (!res.ok) throw new Error(String(res.status));
		body = (await res.json()) as ContextResponse;
	} catch {
		return { status: 'error', reason: 'unreadable', conversation: [], notes: [], attachments: [] };
	}
	return {
		status: body.status, reason: body.reason ?? undefined, repliedTo: body.replied_to ?? undefined,
		answered: body.answered ?? undefined, conversation: body.conversation ?? [], unreadable: body.unreadable,
		attachments: body.attachments ?? [], notes: body.notes ?? [],
	};
}
