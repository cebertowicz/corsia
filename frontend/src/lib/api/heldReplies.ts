/** The person's decision on a held reply: send its stored text once, or send nothing. */

export type HeldVerb = 'approve' | 'decline';

export type HeldOutcome =
	| { status: 'sent' | 'declined' }
	/** Approved, but the recipient no longer exists: the server's line says so. */
	| { status: 'not_sent'; line: string }
	/** Another page decided it first, or it is gone: nothing was sent by this call. */
	| { status: 'settled' }
	| { status: 'unauthorized' }
	/** Delivery failed; the server keeps the reply held and names the failure when it can. */
	| { status: 'error'; line: string | null };

export async function decideHeldReply(bearer: string, id: string, verb: HeldVerb): Promise<HeldOutcome> {
	let res: Response;
	try {
		res = await fetch(`/api/held-replies/${encodeURIComponent(id)}/${verb}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ token: bearer }),
		});
	} catch {
		return { status: 'error', line: null };
	}
	if (res.status === 401) return { status: 'unauthorized' };
	if (res.status === 404 || res.status === 409) return { status: 'settled' };
	if (!res.ok) {
		const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
		return { status: 'error', line: typeof body.detail === 'string' ? body.detail : null };
	}
	const body = (await res.json()) as { state?: string; line?: string };
	if (body.state === 'not_sent') return { status: 'not_sent', line: body.line ?? '' };
	return { status: verb === 'approve' ? 'sent' : 'declined' };
}
