import { writable, type Writable } from 'svelte/store';
import { admitTranscriptRows } from '../chatCommentInputs.svelte';
import type { Message } from './threads';

const transcript = writable<Message[]>([]);
/** The chat's rows. A comment-only input is no chat message: whatever path brings one, it goes to its comment's thread instead. */
export const messages: Writable<Message[]> = {
	subscribe: transcript.subscribe,
	set: rows => transcript.set(admitTranscriptRows(rows)),
	update: change => transcript.update(rows => admitTranscriptRows(change(rows))),
};
