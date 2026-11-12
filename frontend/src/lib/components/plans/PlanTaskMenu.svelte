<script lang="ts">
	import type { OperatorNode, OperatorUserAction } from '$lib/api/plans';
	import Menu from '$lib/components/ui/Menu.svelte';
	import { dismissCommentAttention } from '$lib/api/docs';
	import { DISMISS_ALL, isCommentTask } from '$lib/components/comments/commentTask';

	let {
		node,
		x,
		y,
		onAction,
		onClose,
	}: {
		node: OperatorNode;
		x: number;
		y: number;
		/** Resolves to the server's refusal text, or null once the action landed. */
		onAction: (action: OperatorUserAction) => Promise<string | null> | void;
		onClose: () => void;
	} = $props();

	const LABELS: Record<OperatorUserAction, string> = {
		mark_done: 'Mark done',
		pause_node: 'Pause',
		resume_node: 'Resume',
		close_node: 'Won’t do',
	};

	let busy = $state(false);
	let error = $state<string | null>(null);

	const stopsWorker = $derived(
		node.child?.available === true && node.user_actions.some((a) => a === 'close_node' || a === 'mark_done'),
	);
	const items = $derived([
		...(isCommentTask(node) ? [] : node.user_actions.map((action) => ({
			id: action, label: LABELS[action], danger: action === 'close_node',
		}))),
		...(node.comment_attention ? [{ id: 'dismiss_comments', label: DISMISS_ALL, danger: false }] : []),
	]);

	/** Every action, Won't do included, is one tap: no prompt stands in front of it. */
	async function run(action: string) {
		if (busy) return;
		busy = true;
		error = null;
		try {
			if (action === 'dismiss_comments' && node.comment_attention) {
				await dismissCommentAttention(node.comment_attention.artifact_id);
			} else {
				const failure = await onAction(action as OperatorUserAction);
				if (failure) { error = failure; return; }
			}
			onClose();
		} catch {
			error = 'Could not dismiss comments. Try again.';
		} finally {
			busy = false;
		}
	}
</script>

<Menu
	ariaLabel={`Actions for ${node.title}`}
	{items}
	{x}
	{y}
	{busy}
	note={stopsWorker ? 'Marking this done or Won’t do also stops the agent working on it.' : null}
	{error}
	onSelect={(id) => run(id)}
	{onClose}
/>
