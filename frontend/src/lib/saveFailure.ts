import { plainErrorText } from '$lib/plainError';

const prefix = 'Couldn’t save this change.';
const terminal = /[.!?]$/;

/** Turn one failed setting write into a single, actionable line. A server-written reason carries its own next step. */
export function saveFailureText(detail: unknown, reachedCorsia: boolean): string {
	if (!reachedCorsia) return `${prefix} Check your connection and try again.`;

	const reason = plainErrorText(detail, '');
	if (!reason) return `${prefix} Try again.`;
	return `${prefix} ${terminal.test(reason) ? reason : `${reason}.`}`;
}
