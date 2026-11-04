import { flushSync, mount, unmount } from 'svelte';
import { expect, it, vi } from 'vitest';
import TranscriptContent from './TranscriptContent.svelte';
import { settleClientErrors } from '$lib/ws/clientErrors';
import type { Message } from '$lib/stores/threads';

it('replaces a client error with its saved row without animating the removed row', async () => {
 const client: Message = { id: 'client', thread_id: 't', role: 'system', content: 'Error: failed', created_at: '2026-01-01T00:00:00Z', _clientErrorTurnId: 'turn' };
 const saved: Message = { id: 'saved', thread_id: 't', role: 'system', content: 'failed', created_at: '2026-01-01T00:00:01Z', seq: 1, metadata: { type: 'llm_error', turn_id: 'turn' } };
 const entry = (message: Message) => ({ kind: 'message' as const, id: `message-${message.id}`, message, showActivity: true, showContent: true });
 const props = $state({ entries: [entry(client)], expandedTools: new Set<string>(), expandedReasoning: new Set<string>(), onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {}, threadId: 't', renderedStreaming: false, historyComplete: true });
 const host = document.createElement('div'); document.body.appendChild(host);
 document.documentElement.style.setProperty('--dur-tight', '100');
 document.documentElement.style.setProperty('--dur-standard', '200');
 const animate = vi.spyOn(HTMLElement.prototype, 'animate');
 const component = mount(TranscriptContent, { target: host, props });
 flushSync();
 expect(host.querySelectorAll('[data-message-id]')).toHaveLength(1);
 const rows = settleClientErrors([props.entries[0].message, saved]);
 props.entries = rows.map(entry);
 flushSync();
 await Promise.resolve();
 expect(host.querySelectorAll('[data-message-id]')).toHaveLength(1);
 expect(host.querySelector('[data-message-id]')?.getAttribute('data-message-id')).toBe('saved');
 expect(animate).not.toHaveBeenCalled();
 await unmount(component); animate.mockRestore(); host.remove();
 document.documentElement.style.removeProperty('--dur-tight');
 document.documentElement.style.removeProperty('--dur-standard');
});
