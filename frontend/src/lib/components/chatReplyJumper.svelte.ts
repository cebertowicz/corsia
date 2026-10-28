import { onDestroy } from 'svelte';
import { fromStore } from 'svelte/store';
import { activeThreadId } from '$lib/stores/threads';
import { createChatReplyJumper, type JumpPort } from './chatReplyJump';

/** Resume queued navigation when the selected thread or pager recovers. */
export function useChatReplyJumper(port: JumpPort) {
	const jumper = createChatReplyJumper(port);
	const activeThread = fromStore(activeThreadId);
	$effect(() => {
		activeThread.current; port.pager().loading; port.pager().error;
		jumper.resume();
	});
	onDestroy(jumper.cancel);
	return jumper.jump;
}
