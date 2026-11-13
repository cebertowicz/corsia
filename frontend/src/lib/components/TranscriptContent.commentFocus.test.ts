import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { messages } from '$lib/stores/threads';
import { coreSettings } from '$lib/stores/settings';
import TranscriptContent from './TranscriptContent.svelte';
import { buildTranscriptPresentation } from './transcriptFocusModel';

let instance: Record<string, unknown> | undefined;
afterEach(() => { if (instance) unmount(instance); instance = undefined; messages.set([]); coreSettings.set(null as any); document.body.innerHTML = ''; });

// The server never hands chat a comment-only send, so the transcript holds the commented target and the work.
it('shows a comment target when the combined run is expanded', () => {
  const base = { thread_id: 'thread', created_at: '2026-11-09T17:00:00Z' };
  const rows: any[] = [
    { ...base, id: 'target', role: 'assistant', content: 'Target text', metadata: {}, chat_comments: [{
      id: 'root-id', cid: 'root', user_message_id: 'root-message', quote: 'Target', note: 'Note', from_off: 0, to_off: 6,
      anchor_prefix: '', anchor_suffix: '', created_at: base.created_at,
      answers: [{ author_kind: 'user', user_message_id: 'send', cid: 'answer', text: 'Reply', created_at: base.created_at, unread: false }],
    }] },
    { ...base, id: 'work', role: 'assistant', content: 'Work', metadata: {} },
    { ...base, id: 'last', role: 'assistant', content: 'Done', metadata: { for_user: true } },
  ];
  messages.set(rows);
  coreSettings.set({ features: { focus_mode: true } } as any);
  const present = buildTranscriptPresentation(get(messages) as any[], true);
  const host = document.createElement('div'); document.body.appendChild(host);
  instance = mount(TranscriptContent, { target: host, props: {
    entries: present, threadId: 'thread', renderedStreaming: false, historyComplete: true,
    expandedTools: new Set<string>(), expandedReasoning: new Set<string>(), onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {},
  }});
  flushSync();
  expect(host.querySelectorAll('.fold-toggle')).toHaveLength(1);
  (host.querySelector('.fold-toggle') as HTMLButtonElement).click();
  flushSync();
  expect(host.querySelectorAll('.fold-toggle')).toHaveLength(1);
  expect(host.querySelector('.fold-toggle')?.getAttribute('aria-expanded')).toBe('true');
  expect(host.querySelector('[data-message-id="target"]')).not.toBeNull();
});
