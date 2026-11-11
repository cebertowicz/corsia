import { describe, expect, it } from 'vitest';
import { buildTranscriptPresentation } from './transcriptFocusModel';

const work = (id: string, turn: string, hidden = false) => ({
  id, thread_id: 'thread', role: 'assistant' as const, content: '', created_at: '2026-01-01T00:00:00Z',
  metadata: { turn_id: turn, llm_tool_calls: [{ id: 'call' }] }, commentOnlyReceipt: hidden,
});

describe('live comment-only receipts', () => {
  it('hides only the completed comment turn and keeps adjacent ordinary work', () => {
    const entries = buildTranscriptPresentation([work('ordinary', 'ordinary'), work('comment', 'comment', true)], true);
    const fold = entries.find(entry => entry.kind === 'fold');
    expect(fold?.turnIds).toEqual(['ordinary']);
    expect(fold?.items.map(item => item.message.id)).toEqual(['ordinary']);
  });


  it('shows Working until a terminal fact arrives', () => {
    const entries = buildTranscriptPresentation([work('comment', 'comment')], true, { turnId: 'comment' });
    expect(entries.find(entry => entry.kind === 'fold')?.live).toBe(true);
  });
});
