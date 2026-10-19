import { plainErrorText } from '$lib/plainError';

const prefix = "Couldn't save this change.";
const terminal = /[.!?]$/;
const retry = /try again/i;

/** Turn one failed setting write into a single, actionable line. */
export function saveFailureText(detail: unknown, reachedCorsia: boolean): string {
	if (!reachedCorsia) return `${prefix} Check your connection and try again.`;

	const reason = plainErrorText(detail, '');
	if (!reason) return `${prefix} Try again.`;

	const punctuated = terminal.test(reason) ? reason : `${reason}.`;
	return retry.test(reason) ? `${prefix} ${punctuated}` : `${prefix} ${punctuated} Try again.`;
}
