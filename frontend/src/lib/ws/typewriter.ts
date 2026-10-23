import { get } from 'svelte/store';
import { messages } from '../stores/threads';
import { isDisplayedThread } from '../stores/displayedThreads';
import { emit } from './events';
import { isCurrentTurn, type TurnIdentity } from './turnState';

const TYPEWRITER_BASE_SPEED = 12;
const TYPEWRITER_INTERVAL_MS = 33;
const TYPEWRITER_SPEEDUP_AFTER = 200;

interface TypewriterTimer {
	identity?: TurnIdentity;
	timer: ReturnType<typeof setInterval>;
}

const _typewriters: Map<string, Map<string, TypewriterTimer>> = new Map();

export function _resetTypewritersForTests() {
	for (const inner of _typewriters.values()) {
		for (const record of inner.values()) clearInterval(record.timer);
	}
	_typewriters.clear();
}

export function _getActiveTypewriterCount(threadId?: string): number {
	if (threadId) return _typewriters.get(threadId)?.size ?? 0;
	let total = 0;
	for (const inner of _typewriters.values()) total += inner.size;
	return total;
}

function _registerTimer(
	threadId: string,
	messageId: string,
	record: TypewriterTimer,
) {
	let inner = _typewriters.get(threadId);
	if (!inner) {
		inner = new Map();
		_typewriters.set(threadId, inner);
	}
	inner.set(messageId, record);
}

function _unregisterTimer(threadId: string, messageId: string) {
	const inner = _typewriters.get(threadId);
	if (!inner) return;
	inner.delete(messageId);
	if (inner.size === 0) _typewriters.delete(threadId);
}

/** Whether a reveal is currently running for this exact message. */
export function isRevealing(threadId: string, messageId: string): boolean {
	return _typewriters.get(threadId)?.has(messageId) ?? false;
}

export function startTypewriter(
	threadId: string,
	messageId: string,
	startPos: number,
	endPos: number,
	identity?: TurnIdentity,
	pacing: 'characters' | 'words' = 'characters',
) {
	const existing = _typewriters.get(threadId)?.get(messageId);
	if (existing) clearInterval(existing.timer);

	let pos = startPos;
	// The scripted opening uses the same timer/store path as settled step text,
	// but advances at word boundaries at one provider-independent reading pace.
	const content = get(messages).find(m => m.id === messageId)?.content ?? '';
	const wordEnds = pacing === 'words'
		? Array.from(content.matchAll(/\S+\s*/g), match => match.index! + match[0].length)
			.filter(end => end > startPos && end <= endPos)
		: [];
	let wordIndex = 0;

	// Hiding content and animating it are ONE act, so they happen in one place.
	// `handleStepText` used to set `typewriterPos: 0` on the row and then start
	// a reveal only when the row was newly inserted — so a message that already
	// existed (history load, an earlier `message_added`, a re-delivered
	// snapshot) was hidden with nothing running to un-hide it. No timer meant
	// `finish()` could never fire either. That is the reported "the last output
	// disappears and comes back when the next one starts streaming": it was
	// waiting for any unrelated update to rewrite the row.
	messages.update(msgs =>
		msgs.map(m => (m.id === messageId ? {
			...m, typewriterPos: startPos,
			...(pacing === 'words' ? { firstRunOpeningReveal: true } : {}),
		} : m)),
	);

	/** Stop animating and show the message WHOLE. Never leave it part-revealed.
	 *
	 * `typewriterPos` is how much of a message is currently on screen, so a
	 * reveal that stops early leaves the rest invisible — the message sits
	 * intact in the store and the user sees nothing, until some later render
	 * happens to advance it. That is the reported "my message disappeared and
	 * came back when the next one started streaming": the animation was
	 * abandoned at position 0 when its turn ended, and a message that was never
	 * streamed live starts at 0.
	 *
	 * The animation is decoration. It may stop whenever it likes; it may never
	 * be the reason content is missing.
	 */
	const finish = () => {
		clearInterval(timer);
		_unregisterTimer(threadId, messageId);
		messages.update(msgs =>
			msgs.map(m =>
				m.id === messageId && m.typewriterPos !== undefined
					? { ...m, typewriterPos: undefined }
					: m
			)
		);
	};

	const timer = setInterval(() => {
		if (
			!isDisplayedThread(threadId)
			|| (identity !== undefined && !isCurrentTurn(threadId, identity))
		) {
			finish();
			return;
		}

		const revealed = pos - startPos;
		const speed = revealed > TYPEWRITER_SPEEDUP_AFTER
			? TYPEWRITER_BASE_SPEED + Math.floor((revealed - TYPEWRITER_SPEEDUP_AFTER) / 100) * 4
			: TYPEWRITER_BASE_SPEED;

		pos = pacing === 'words'
			? wordEnds[wordIndex++] ?? endPos
			: Math.min(pos + speed, endPos);

		const current = get(messages);
		const targetExists = current.some(m => m.id === messageId);
		if (!targetExists) {
			clearInterval(timer);
			_unregisterTimer(threadId, messageId);
			return;
		}

		messages.update(msgs =>
			msgs.map(m =>
				m.id === messageId
					? { ...m, typewriterPos: pos >= endPos ? undefined : pos }
					: m
			)
		);
		emit('typewriter_tick', { messageId, threadId });
		if (pos >= endPos) {
			clearInterval(timer);
			_unregisterTimer(threadId, messageId);
			emit('typewriter_done', { messageId, threadId });
		}
	}, pacing === 'words' ? 90 : TYPEWRITER_INTERVAL_MS);

	_registerTimer(threadId, messageId, { identity, timer });
}

export function cancelThreadTypewriters(
	threadId: string,
	identity?: TurnIdentity,
) {
	const inner = _typewriters.get(threadId);
	const cancelledMessageIds = new Set<string>();
	if (inner) {
		for (const [messageId, record] of inner) {
			if (
				identity
				&& (record.identity?.turnId !== identity.turnId || record.identity?.turnOrder !== identity.turnOrder)
			) continue;
			clearInterval(record.timer);
			inner.delete(messageId);
			cancelledMessageIds.add(messageId);
		}
		if (inner.size === 0) _typewriters.delete(threadId);
	}
	if (cancelledMessageIds.size === 0) return;
	messages.update(msgs =>
		msgs.map(m =>
			m.thread_id === threadId
				&& cancelledMessageIds.has(m.id)
				&& m.typewriterPos !== undefined
				? { ...m, typewriterPos: undefined }
				: m
		)
	);
}
