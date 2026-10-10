import {
	buildPendingContextDigest,
	canMergeRows,
	contextEventRows,
	sourceMembers,
	sourceRange,
	isCompactTranscriptEvent,
	isPassiveContextEvent,
	isPendingPassiveContextEvent,
	mergedRowFacts,
	presentContextDigest,
	summarizeContextEvents,
} from './contextEventModel';

export {
	buildPendingContextDigest,
	isActivityReceipt,
	isPassiveContextEvent,
	isPendingPassiveContextEvent,
	passiveContextEventLabel,
	summarizeContextEvents,
} from './contextEventModel';

/**
 * The pure shaping rules behind the transcript.
 *
 * Extracted from `MessageList.svelte` so the component holds rendering and this
 * holds "what the transcript is", and so both stay a size a reader can keep in
 * their head. Nothing here touches a store or the DOM.
 */
export type TranscriptMessage = Record<string, any>;

/** A canonical artifact-removal chat event (hide, restore, release, or rehome-away). */
export function isArtifactRemovalEvent(msg: TranscriptMessage): boolean {
	return msg.role === 'system' && msg.metadata?.type === 'artifact_removed';
}

/** The single-line label for one ungrouped artifact-removal event. */
export function artifactRemovalEventLabel(msg: TranscriptMessage): string {
	return String(msg.content ?? '');
}

/** Per-subtype past-tense verb, matching backend's `_REMOVAL_SUMMARIES` templates. */
const REMOVAL_VERBS: Record<string, string> = {
	hidden_from_pane: 'Removed',
	shown_in_pane: 'Restored',
	ownership_released: 'Released',
	ownership_rehomed: 'Moved',
};

/**
 * One concise line for N removal events sharing one causal batch identity.
 *
 * A causal batch can mix subtypes (e.g. one turn hides one artifact and
 * restores another) — the verb must reflect what actually happened, never
 * default to "Removed" for a batch that isn't uniformly a removal.
 */
export function summarizeArtifactRemovalEvents(group: TranscriptMessage[]): string {
	if (group.length === 1) return artifactRemovalEventLabel(group[0]);
	const subtypes = new Set(group.map((msg) => msg.metadata?.subtype));
	const verb = subtypes.size === 1 ? REMOVAL_VERBS[[...subtypes][0] as string] : undefined;
	if (!verb) return `${group.length} pane changes`;
	const names = group
		.map((msg) => msg.metadata?.title)
		.filter((title): title is string => typeof title === 'string' && title.length > 0);
	const preview = names.slice(0, 2).join(', ');
	const rest = group.length - Math.min(2, names.length);
	const suffix = rest > 0 ? ` +${rest} more` : '';
	return names.length > 0
		? `${verb} ${preview}${suffix}`
		: `${group.length} artifacts ${verb.toLowerCase()}`;
}

function isToolOnlyAssistantStep(msg: TranscriptMessage): boolean {
	return msg.role === 'assistant'
		&& msg.content === ''
		&& Array.isArray(msg.metadata?.tool_calls)
		&& msg.metadata.tool_calls.length > 0
		&& typeof msg.metadata?.turn_id === 'string'
		&& msg.metadata.turn_id.length > 0;
}

export function collapseToolOnlyAssistantSteps(
	rows: TranscriptMessage[],
): TranscriptMessage[] {
	const collapsed: TranscriptMessage[] = [];
	for (let index = 0; index < rows.length; index += 1) {
		const first = rows[index];
		if (!isToolOnlyAssistantStep(first)) {
			collapsed.push(first);
			continue;
		}

		const group = [first];
		while (
			index + 1 < rows.length
			&& isToolOnlyAssistantStep(rows[index + 1])
			&& rows[index + 1].metadata.turn_id === first.metadata.turn_id
			&& canMergeRows(first, rows[index + 1])
		) {
			group.push(rows[index + 1]);
			index += 1;
		}
		if (group.length === 1) {
			collapsed.push(first);
			continue;
		}

		const toolHints = group.flatMap(
			(step) => step.toolHints ?? step.metadata?.tool_hints ?? [],
		);
		collapsed.push({
			...first,
			...mergedRowFacts(group),
			seq: undefined,
			created_at: group.at(-1)?.created_at ?? first.created_at,
			toolHints,
			metadata: {
				...first.metadata,
				tool_hints: toolHints,
				tool_calls: group.flatMap((step) => step.metadata.tool_calls),
			},
		});
	}
	return collapsed;
}

/**
 * Collapse consecutive artifact-removal events that share one non-null
 * causal batch identity into a single row. Never groups by adjacency or
 * count alone — a message without a causal_event_id, or one whose neighbor
 * carries a different id, always renders on its own line.
 */
export function groupArtifactRemovalEvents(
	rows: TranscriptMessage[],
): TranscriptMessage[] {
	const next: TranscriptMessage[] = [];
	for (let i = 0; i < rows.length; i += 1) {
		const msg = rows[i];
		const causalId = msg.metadata?.causal_event_id;
		if (!isArtifactRemovalEvent(msg) || !causalId) {
			next.push(msg);
			continue;
		}
		const group = [msg];
		while (
			i + 1 < rows.length
			&& isArtifactRemovalEvent(rows[i + 1])
			&& rows[i + 1].metadata?.causal_event_id === causalId
			&& canMergeRows(msg, rows[i + 1])
		) {
			group.push(rows[i + 1]);
			i += 1;
		}
		if (group.length === 1) {
			next.push(msg);
			continue;
		}
		next.push({
			id: `artifact-removal-digest-${group[0].id}-${group.at(-1)?.id}`,
			...mergedRowFacts(group),
			thread_id: msg.thread_id,
			role: 'system',
			content: summarizeArtifactRemovalEvents(group),
			metadata: {
				type: 'artifact_removed',
				event_type: 'artifact.removal_digest',
				causal_event_id: causalId,
				removal_events: group.map((item) => ({
					id: item.id,
					content: item.content,
					metadata: item.metadata,
					created_at: item.created_at,
				})),
			},
			created_at: group.at(-1)?.created_at ?? msg.created_at,
		});
	}
	return next;
}

/** The rows the transcript renders, in order, with digests and tool groups applied. */
export function buildVisibleMessages(
	allMessages: TranscriptMessage[],
	barriers: readonly TranscriptMessage[] = [],
): TranscriptMessage[] {
	if (barriers.length) {
		const stops = new Set(barriers.map((row) => row.id));
		const visible: TranscriptMessage[] = [];
		let part: TranscriptMessage[] = [];
		for (const row of allMessages) {
			part.push(row);
			if (stops.has(row.id)) {
				visible.push(...buildVisibleMessages(part));
				part = [];
			}
		}
		return [...visible, ...buildVisibleMessages(part)];
	}
	const rows = groupArtifactRemovalEvents(allMessages.filter((m) => (
		m.metadata?.type !== 'seed'
		&& !(m.role === 'user' && m.metadata?.routed_event)
	)));
	const next: TranscriptMessage[] = [];
	for (let i = 0; i < rows.length; i += 1) {
		const msg = rows[i];
		if (!isCompactTranscriptEvent(msg)) {
			next.push(msg);
			continue;
		}
		const run = [msg];
		while (
			i + 1 < rows.length
			&& isCompactTranscriptEvent(rows[i + 1])
			&& canMergeRows(msg, rows[i + 1])
		) {
			run.push(rows[i + 1]);
			i += 1;
		}
		// A digest with nothing for a person in it is not a row, so it neither
		// shows nor counts toward the run that gets one Context chip.
		const group = run.flatMap((item) => presentContextDigest(item) ?? []);
		if (group.length < 3) {
			next.push(...group);
			continue;
		}
		const includesContext = group.some(isPassiveContextEvent);
		const summary = summarizeContextEvents(group);
		next.push({
			id: `context-digest-${group[0].id}-${group.at(-1)?.id}`,
			...mergedRowFacts(group),
			thread_id: msg.thread_id,
			role: 'system',
			content: summary,
			metadata: {
				type: includesContext ? 'context_event' : 'activity_receipt',
				event_type: 'context.digest',
				event_disposition: includesContext ? 'passive' : undefined,
				summary,
				context_events: group.flatMap(contextEventRows),
			},
			created_at: group.at(-1)?.created_at ?? msg.created_at,
			delivery_state: group.every((item) => item.delivery_state === 'delivered')
				? 'delivered' : group.at(-1)?.delivery_state,
		});
	}
	return collapseToolOnlyAssistantSteps(next);
}

/** Rebuild disjoint original-member slices through the existing reducers, once per cut. */
export function partitionMessageAtCards(
	message: TranscriptMessage,
	cuts: readonly number[],
): TranscriptMessage[] {
	const members = sourceMembers(message);
	const range = sourceRange(message);
	if (members.length < 2 || !range) return [message];
	const inside = cuts.filter((cut) => cut >= range.min && cut < range.max);
	if (!inside.length) return [message];
	const parts: TranscriptMessage[] = [];
	let start = 0;
	for (const cut of inside) {
		let end = start;
		while (end < members.length && members[end].seq <= cut) end += 1;
		if (end > start) parts.push(...buildVisibleMessages(members.slice(start, end)));
		start = end;
	}
	if (start < members.length) parts.push(...buildVisibleMessages(members.slice(start)));
	return parts;
}

/** Shape one transcript boundary without assigning sequence to passive input. */
export function buildMessageListPresentation(
	rows: TranscriptMessage[],
	barriers: readonly TranscriptMessage[] = [],
) {
	const pending = rows.filter(isPendingPassiveContextEvent);
	return {
		visibleMessages: buildVisibleMessages(
			rows.filter((msg) => !isPendingPassiveContextEvent(msg)),
			barriers,
		),
		pendingContextDigest: buildPendingContextDigest(pending),
	};
}

export { buildTranscriptPresentation } from './transcriptFocusModel';
export type {
	TranscriptDividerEntry,
	TranscriptEntry,
	TranscriptFoldEntry,
	TranscriptMessageEntry,
	TranscriptPauseEntry,
	TranscriptUpdateEntry,
} from './transcriptFocusModel';
