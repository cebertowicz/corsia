import type { SaveOffer } from '$lib/api/savedLoginOffer';

const question = (offer: SaveOffer, domain: string) => offer.update ? `Update the saved ${domain} login with this password?` : `Save the ${domain} login for agents?`;

/** Every string of the save-login offer the pane itself words; a refusal's sentence comes from the server. */
export const SAVE_OFFER_TEXT = {
	save: 'Save',
	update: 'Update',
	notNow: 'Not now',
	close: 'Close',
	saved: 'Saved',
	updated: 'Updated',
	error: 'Couldn’t save the login. Try again.',
	question,
	/** What the screen reader hears as the dock appears: the question with the site, then the username, as the screen shows them. */
	announce: (offer: SaveOffer) => `${question(offer, offer.domain)} ${offer.username}`,
} as const;

/** The username is cut in the middle, keeping both ends, only when the pane measures that it would not fit. The domain is never cut. */
export function shortenMiddle(text: string, max: number): string {
	if (text.length <= max) return text;
	const keep = max - 1;
	const head = Math.ceil(keep / 2);
	return `${text.slice(0, head)}…${text.slice(text.length - (keep - head))}`;
}
