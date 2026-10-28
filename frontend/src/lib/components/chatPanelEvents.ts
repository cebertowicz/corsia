import { get, type Readable } from 'svelte/store';
import { on } from '$lib/ws';
import { loadCheckpoints } from '$lib/stores/checkpoints';
import { hydrateSubagentThread } from '$lib/stores/subagents';

export async function refreshVisibleThread(threadId: string, bearer: string | null, page: Promise<void>): Promise<void> {
	try {
		await Promise.all([
			page,
			bearer ? loadCheckpoints(threadId, bearer).catch(() => null) : Promise.resolve(null),
			bearer ? hydrateSubagentThread(threadId, bearer).catch(() => null) : Promise.resolve(null),
		]);
	} catch { /* ignore */ }
}

export interface ChatPanelEventPort {
	activeThreadId: Readable<string | null>;
	isNearBottom: () => boolean;
	scrollToBottom: () => void;
	followBottom: () => void;
	fetchThreadMessages: (threadId: string) => Promise<void>;
	clearWarmKeys: (threadId: string) => void;
}

/** Subscribe the panel to transcript-affecting live events and return cleanup. */
export function subscribeChatPanelEvents(port: ChatPanelEventPort): () => void {
	const followIfActive = (data: any) => {
		if (data?.thread_id === get(port.activeThreadId) && port.isNearBottom()) port.scrollToBottom();
	};
	const unsubStep = on('step_text', followIfActive);
	const unsubTool = on('tool_start', followIfActive);
	const unsubToolDone = on('tool_done', followIfActive);
	const unsubReasoning = on('step_reasoning', followIfActive);
	const unsubTextDelta = on('text_delta', followIfActive);
	const unsubTypewriter = on('typewriter_tick', () => {
		if (port.isNearBottom()) port.followBottom();
	});
	const refreshLifecycle = (data: any) => {
		const sid = data?.thread_id;
		if (!sid || sid !== get(port.activeThreadId) || data?.error_type === 'stop_failed') return;
		void port.fetchThreadMessages(sid);
	};
	const handleTurnTerminal = (data: any) => {
		const sid = data?.thread_id;
		if (!sid || typeof data?.turn_id !== 'string') return;
		port.clearWarmKeys(sid);
		refreshLifecycle(data);
		if (sid === get(port.activeThreadId) && port.isNearBottom()) port.scrollToBottom();
	};
	const unsubTurnComplete = on('turn_complete', handleTurnTerminal);
	const unsubTurnCancelled = on('turn_cancelled', handleTurnTerminal);
	const unsubTurnInterrupted = on('turn_interrupted', handleTurnTerminal);
	const unsubTurnError = on('error', handleTurnTerminal);
	const unsubTurnActivity = on('turn_activity', refreshLifecycle);
	const unsubConnected = on('connected', () => {
		const sid = get(port.activeThreadId);
		if (sid) void port.fetchThreadMessages(sid);
	});
	return () => {
		unsubStep();
		unsubTool();
		unsubToolDone();
		unsubReasoning();
		unsubTextDelta();
		unsubTypewriter();
		unsubTurnComplete();
		unsubTurnCancelled();
		unsubTurnInterrupted();
		unsubTurnError();
		unsubTurnActivity();
		unsubConnected();
	};
}
