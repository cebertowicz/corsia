import { captureComment, IdentityInUseError, type CapturedComment, type CommentSelection } from './commentSelection';
import { showPosted, type DraftVersion } from './docComments';
import { timed } from './commentPostTiming';

export type LivePress = {
	selection: CommentSelection;
	/** The draft's one id, the same for every press of Comment on it. */
	clientId: string;
	/** An earlier press used this id, so it may have stored the comment already. */
	again: boolean;
	seq: number;
	signal: AbortSignal;
	version: DraftVersion;
};

/**
 * A live document's comment, captured and posted under its draft's one id, so no press of Comment stores it twice.
 * A press after an earlier one first looks for the comment that press may have stored. A capture the server refuses
 * because the id is already in use looks again: an earlier press's post stored it meanwhile. Either way a stored
 * comment shows and is this press's result (or `EarlierVersionPosted` when the draft changed since). A failed post
 * gives the captured range back.
 */
export async function postLive(press: LivePress, post: (captured: CapturedComment) => Promise<string>): Promise<string> {
	const { selection, clientId, version } = press;
	const artifactId = selection.session.artifactId;
	if (press.again) {
		const id = await showPosted(artifactId, clientId, version);
		if (id) return id;
	}
	let captured: CapturedComment;
	try {
		captured = await timed(clientId, 'cap', () => captureComment(selection, press.seq, press.signal, clientId));
	} catch (error) {
		if (!(error instanceof IdentityInUseError)) throw error;
		const id = await showPosted(artifactId, clientId, version);
		if (id) return id;
		throw error;
	}
	try {
		return await post(captured);
	} catch (error) {
		selection.session.releaseReservation(captured.clientId);
		throw error;
	}
}
