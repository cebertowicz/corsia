import type { SaveOffer } from '$lib/api/savedLoginOffer';

/** Every string of the save-login offer, approved by Product Experience (Nov 24). */
export const SAVE_OFFER_TEXT = {
	announce: 'CORSIA can save this login',
	save: 'Save',
	update: 'Update',
	notNow: 'Not now',
	saved: 'Saved',
	updated: 'Updated',
	error: 'Couldn’t save the login. Try again.',
	question: (offer: SaveOffer, domain: string) => offer.update ? `Update the saved ${domain} login?` : `Save the ${domain} login for agents?`,
} as const;

/** Past this many characters a name is cut in the middle, keeping both ends. A domain gets two lines of the question, so it gets more than a username. */
export const MAX_DOMAIN = 48;
export const MAX_USERNAME = 32;

export function shortenMiddle(text: string, max: number): string {
	if (text.length <= max) return text;
	const keep = max - 1;
	const head = Math.ceil(keep / 2);
	return `${text.slice(0, head)}…${text.slice(text.length - (keep - head))}`;
}
