<script lang="ts">
	import { get } from 'svelte/store';
	import { onDestroy } from 'svelte';
	import AvatarStack from './ui/AvatarStack.svelte';
	import type { CommentFeedback } from '$lib/api/docs';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import {
		prepareCommentArrival, revealCommentArrival, type PreparedCommentArrival,
	} from '$lib/stores/docCommentArrivals';
	import { commentArrivalDelay, commentHoldMs, selectSweepMs } from '$lib/typingPace';
	import { displayedDocument } from '$lib/ws/presence';
	import { arrivalSelectIn } from '$lib/cm/typed-reveal-arrival';
	import { userParticipant } from '$lib/stores/participants';
	import {
		agentColor, commentAnchor, docPresence, isViewing, presenceDetail, presenceFor,
		type DocActivityFrame,
	} from '$lib/docPresence';
	import {
		attachDocPresenceSurface, notifyDocPresenceViewingChanged, revealPendingArrival,
		type PreparedPresenceArrival,
	} from '$lib/docPresenceRuntime';
	import type { PresenceRange } from '$lib/cm/presence-marks';

	type DocState = 'loading' | 'ready' | 'error';
	type PresenceParticipant = {
		id: string;
		name: string;
		color: string;
		detail?: string;
		active?: boolean;
	};
	type CleanReload = () => void | Promise<void>;
	type PresencePayload =
		| { kind: 'edit'; reveal: CleanReload }
		| { kind: 'comment'; arrival: PreparedCommentArrival }
		| { kind: 'blocked' };

	let {
		target,
		artifactId = null,
		docState = 'loading',
		root = null,
		ranges = $bindable<PresenceRange[]>([]),
		onPrepareCleanReload,
		onFeedback,
		onArrival,
	}: {
		target: DocViewerTarget;
		artifactId?: string | null;
		docState?: DocState;
		/** The document canvas this status sits beside; its editor, when mounted, runs a comment's select. */
		root?: HTMLElement | null;
		ranges?: PresenceRange[];
		onPrepareCleanReload: () => Promise<CleanReload | null>;
		onFeedback: (artifactId: string, feedback: CommentFeedback) => void;
		onArrival?: (frame: DocActivityFrame) => void;
	} = $props();

	let clock = $state(Date.now());
	let visibility = $state<DocumentVisibilityState>('visible');
	const recentAgents = $derived.by(() => {
		$docPresence;
		return artifactId ? presenceFor(artifactId, clock) : [];
	});
	const viewing = $derived(isViewing(artifactId, target.threadId, $displayedDocument, visibility));
	const participants = $derived.by((): PresenceParticipant[] => {
		if (!viewing) return [];
		return [
			{ id: $userParticipant.id, name: $userParticipant.name, color: $userParticipant.color },
			...recentAgents.map((agent) => ({
				id: agent.id,
				name: agent.name,
				color: agent.color,
				detail: presenceDetail(agent, clock),
				active: agent.arrivalActive,
			})),
		];
	});

	$effect(() => {
		ranges = recentAgents
			.filter((agent) => agent.lastSpan !== null)
			.map((agent) => ({
				threadId: agent.id,
				name: agent.name,
				color: agent.color,
				span: agent.lastSpan!,
				highlightSpan: agent.highlightSpan,
				highlightKind: agent.highlightKind,
				revealPending: artifactId ? () => revealPendingArrival(artifactId, agent.id) : undefined,
			}));
	});

	function activityChars(frame: DocActivityFrame): number {
		return frame.span ? Math.max(0, frame.span.to - frame.span.from) : 0;
	}

	async function prepare(frame: DocActivityFrame, currentArtifactId: string): Promise<PreparedPresenceArrival> {
		if (frame.kind === 'edit') {
			const reveal = await onPrepareCleanReload();
			if (!reveal) {
				return { chars: 0, payload: { kind: 'blocked' } satisfies PresencePayload, highlightSpan: null };
			}
			return { chars: activityChars(frame), payload: { kind: 'edit', reveal } satisfies PresencePayload };
		}
		if (frame.kind === 'comment' || frame.kind === 'suggestion') {
			const arrival = await prepareCommentArrival(currentArtifactId, frame.comment_id);
			const comment = arrival.comment;
			const anchor = commentAnchor(comment);
			// Select the anchor, hold while the agent writes, then the card: timings scale with the anchor and the body.
			const delayMs = commentArrivalDelay(anchor ? anchor.to - anchor.from : activityChars(frame), comment?.text.length ?? 0);
			return {
				chars: comment ? comment.text.length + (comment.replacement?.length ?? 0) : 0,
				delayMs: comment ? delayMs : undefined,
				...(anchor ? { highlightSpan: anchor } : {}),
				payload: { kind: 'comment', arrival } satisfies PresencePayload,
			};
		}
		return { chars: activityChars(frame), payload: { kind: 'blocked' } satisfies PresencePayload };
	}

	/** A comment's select step in the editor beside this status: keyed by the comment, timed by its anchor and body. */
	function select(frame: DocActivityFrame, prepared: PreparedPresenceArrival, currentArtifactId: string) {
		const payload = prepared.payload as PresencePayload | undefined;
		const comment = payload?.kind === 'comment' ? payload.arrival.comment : null;
		const anchor = commentAnchor(comment);
		if (!comment || !anchor) return null;
		const agent = presenceFor(currentArtifactId, Date.now()).find((entry) => entry.id === frame.thread_id);
		return arrivalSelectIn(root, {
			key: comment.id, threadId: frame.thread_id, name: agent?.name ?? frame.agent, color: agent?.color ?? agentColor(frame.thread_id),
			from: anchor.from, to: anchor.to,
			sweepMs: selectSweepMs(anchor.to - anchor.from), holdMs: commentHoldMs(comment.text.length),
			callId: frame.call_id ?? null, insert: comment.replacement ?? null,
		});
	}

	async function reveal(frame: DocActivityFrame, payload: unknown, currentArtifactId: string): Promise<void> {
		const prepared = payload as PresencePayload | undefined;
		if (prepared?.kind === 'edit') {
			await prepared.reveal();
			onArrival?.(frame);
			return;
		}
		if (prepared?.kind === 'comment') {
			revealCommentArrival(currentArtifactId, prepared.arrival);
			onFeedback(currentArtifactId, prepared.arrival.feedback);
			onArrival?.({ ...frame, span: commentAnchor(prepared.arrival.comment) });
		}
	}

	$effect(() => {
		const timer = setInterval(() => { clock = Date.now(); }, 10_000);
		const updateVisibility = () => {
			visibility = document.visibilityState;
			notifyDocPresenceViewingChanged(artifactId);
		};
		updateVisibility();
		document.addEventListener('visibilitychange', updateVisibility);
		return () => {
			clearInterval(timer);
			document.removeEventListener('visibilitychange', updateVisibility);
		};
	});

	$effect(() => {
		const currentArtifactId = artifactId;
		if (!currentArtifactId) return;
		return attachDocPresenceSurface(currentArtifactId, target.instanceId, {
			isViewing: () => isViewing(
				currentArtifactId,
				target.threadId,
				get(displayedDocument),
				document.visibilityState,
			),
			prepare: (frame) => prepare(frame, currentArtifactId),
			reveal: (frame, payload) => reveal(frame, payload, currentArtifactId),
			select: (frame, prepared) => select(frame, prepared, currentArtifactId),
		});
	});

	$effect(() => {
		$displayedDocument;
		visibility;
		notifyDocPresenceViewingChanged(artifactId);
	});

	onDestroy(() => { ranges = []; });
</script>

{#if docState === 'ready' && participants.length}
	<div class="presence-row" aria-label="Recent document activity">
		<AvatarStack people={participants} max={3} />
	</div>
{/if}

<style>
	.presence-row { align-self: flex-end; display: flex; align-items: center; gap: 8px; min-height: 28px; color: var(--text-tertiary); font-size: var(--text-caption); }
</style>
