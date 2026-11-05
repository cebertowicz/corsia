import { getContext, hasContext, setContext } from 'svelte';
import type { DocumentReviewer } from '$lib/api/docReviews';
const key = Symbol('document-mention-roster');
export type MentionRoster = { reviewers: DocumentReviewer[]; loadState: 'loading' | 'ready' | 'error'; rosterError: boolean; load: () => Promise<void> };
const empty: MentionRoster = { reviewers: [], loadState: 'ready', rosterError: false, load: async () => {} };
export function provideMentionRoster(roster: MentionRoster) { setContext(key, roster); }
export function hasMentionRoster(): boolean { return hasContext(key); }
export function useMentionRoster(): MentionRoster { return getContext<MentionRoster>(key) ?? empty; }
