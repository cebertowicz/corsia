/**
 * The one gate between an API's error text and a user's eyes.
 *
 * An HTTP `detail` is written for whoever reads the log. Some are sentences a
 * human wrote for a human and pass through unchanged; a snake_case identifier
 * is a code, and a code on a user surface asks the user a question they hold no
 * evidence to answer — being shown `provider_write_outcome_unknown` and a
 * button is the exact defect this exists to stop.
 */
const MACHINE_CODE = /^[a-z0-9]+(?:_[a-z0-9]+)+$/;

/**
 * What a user is told when the request never reached CORSIA.
 *
 * `String(error)` on a fetch rejection is a runtime's words, not ours — a
 * `TypeError: Failed to fetch` on screen is the same defect as a code on
 * screen. One sentence, one place, so the whole app says it the same way.
 */
export const SERVER_UNREACHABLE =
	'Couldn’t reach the server. Check your connection and try again.';

export function isMachineCode(value: string): boolean {
	return MACHINE_CODE.test(value.trim());
}

/** Return `detail` if it reads as human copy, else the caller's plain sentence. */
export function plainErrorText(detail: unknown, fallback: string): string {
	if (typeof detail !== 'string') return fallback;
	const text = detail.trim();
	if (!text || isMachineCode(text)) return fallback;
	return text;
}

/**
 * Read a failed HTTP response as one sentence for the user.
 *
 * The whole app funnels through here, so the raw detail lands in the console
 * exactly once and the screen never has to be audited a second time. Callers
 * that grew their own copy of this drifted apart — one of them stringified a
 * JSON detail object straight into a toast.
 */
export async function plainResponseError(
	response: Response,
	fallback: string,
): Promise<string> {
	const body = await response.json().catch(() => null);
	if (body?.detail !== undefined) {
		console.error('[api] request rejected', response.status, response.url, body.detail);
	}
	return plainErrorText(body?.detail, fallback);
}
