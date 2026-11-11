import { parseRunStubs, type RunStub } from './focusRuns';

type RunPort = {
  token: string;
  runs: () => readonly RunStub[];
  replace: (old: RunStub, next: RunStub[]) => void;
  request: (url: string) => Promise<{ ok: boolean; json: () => Promise<any> }>;
};

/** Re-read affected stub ranges so their counts remain server-owned. */
export async function refreshCommentRuns(threadId: string, turnId: string, port: RunPort): Promise<void> {
  for (const stub of port.runs().filter(run => run.turnIds.includes(turnId))) {
    const params = new URLSearchParams({ token: port.token, view: 'focus-run', limit: '1',
      from_id: stub.firstId, to_id: stub.lastId });
    try {
      const response = await port.request(`/api/threads/${encodeURIComponent(threadId)}/messages?${params}`);
      if (!response.ok) throw new Error('Focus run refresh failed');
      const page = await response.json();
      if (!port.runs().includes(stub)) continue;
      const next = parseRunStubs(page);
      if (next.length) next[0].continuesOlder = stub.continuesOlder;
      port.replace(stub, next);
    } catch (error) {
      console.error('Focus run refresh failed:', error);
    }
  }
}
