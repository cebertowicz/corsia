import { writable } from 'svelte/store';

/** A reply chip's current target, also used to open a collapsed Focus run. */
export const chatReplyJump = writable<{ threadId: string; messageId: string; seq: number; cid: string } | null>(null);
