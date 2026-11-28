import { writable } from 'svelte/store';
import type { AttentionDestination } from '$lib/api/attentionQueue';

/** A request made from Activity after the destination chat has opened. */
export const activityChatJump = writable<{ threadId: string; destination: AttentionDestination } | null>(null);
