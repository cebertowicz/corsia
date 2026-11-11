import { on } from '$lib/ws';

/** Subscribe only to comment changes that can alter unread attention. */
export function subscribeUnreadAttention(reload: () => void): () => void {
	return on('doc_comment_changed', (frame: unknown) => {
		if (typeof frame === 'object' && frame !== null && 'unread_attention_changed' in frame && frame.unread_attention_changed === true) reload();
	});
}
