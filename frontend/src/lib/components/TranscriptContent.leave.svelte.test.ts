import { flushSync, mount, unmount } from 'svelte';
import { expect, it, vi } from 'vitest';
import TranscriptContent from './TranscriptContent.svelte';
import type { Message } from '$lib/stores/threads';

it('animates a client error row leaving when a later Turn starts', async () => {
 const client: Message = { id: 'client', thread_id: 't', role: 'system', content: 'Error: failed', created_at: '2026-01-01T00:00:00Z', _clientErrorTurnId: 'turn' };
 const props = $state({ entries: [{ kind: 'message' as const, id: 'message-client', message: client, showActivity: true, showContent: true }], expandedTools: new Set<string>(), expandedReasoning: new Set<string>(), onToggleToolBlock: () => {}, onToggleReasoning: () => {}, onOpenLightbox: () => {}, threadId: 't', renderedStreaming: false, historyComplete: true });
 const host = document.createElement('div'); document.body.appendChild(host);
 document.documentElement.style.setProperty('--dur-tight', '100');
 document.documentElement.style.setProperty('--dur-standard', '200');
 const animate = vi.spyOn(HTMLElement.prototype, 'animate');
 const component = mount(TranscriptContent, { target: host, props });
 flushSync(); props.entries = []; flushSync();
 expect(animate).toHaveBeenCalled();
 await unmount(component); animate.mockRestore(); host.remove();
 document.documentElement.style.removeProperty('--dur-tight');
 document.documentElement.style.removeProperty('--dur-standard');
});
