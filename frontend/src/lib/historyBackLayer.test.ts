import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backLayerDepth, pushBackLayer, resetBackLayers } from './historyBackLayer';

/**
 * The browser's own history is not simulated by jsdom's popstate, so these
 * tests drive `popstate` directly and assert the stacking contract: the most
 * recently pushed layer gets first refusal, and consuming a pop stops it from
 * reaching the app's own navigation.
 */
function pop(state: unknown = null): PopStateEvent {
	const event = new PopStateEvent('popstate', { state });
	vi.spyOn(event, 'stopImmediatePropagation');
	window.dispatchEvent(event);
	return event;
}

beforeEach(() => {
	resetBackLayers();
	vi.spyOn(history, 'pushState').mockImplementation(() => {});
	vi.spyOn(history, 'back').mockImplementation(() => {});
});

afterEach(() => {
	resetBackLayers();
	vi.restoreAllMocks();
});

describe('pushBackLayer', () => {
	it('pushes one history entry per layer', () => {
		pushBackLayer(() => true);
		pushBackLayer(() => true);
		expect(history.pushState).toHaveBeenCalledTimes(2);
		expect(backLayerDepth()).toBe(2);
	});

	it('pops the most recent layer first, so a sheet closes before its surface', () => {
		const order: string[] = [];
		pushBackLayer(() => { order.push('surface'); return true; });
		pushBackLayer(() => { order.push('sheet'); return true; });

		pop();
		expect(order).toEqual(['sheet']);
		pop();
		expect(order).toEqual(['sheet', 'surface']);
		expect(backLayerDepth()).toBe(0);
	});

	it('consumes the pop so Back does not also leave the app', () => {
		pushBackLayer(() => true);
		expect(pop().stopImmediatePropagation).toHaveBeenCalledOnce();
	});

	it('lets an unconsumed pop fall through to ordinary navigation', () => {
		pushBackLayer(() => false);
		expect(pop().stopImmediatePropagation).not.toHaveBeenCalled();
	});

	it('unwinds its own history entry when the top layer is disposed', () => {
		const dispose = pushBackLayer(() => true);
		dispose();
		expect(history.back).toHaveBeenCalledOnce();
		expect(backLayerDepth()).toBe(0);
	});

	it('swallows the cleanup pop after direct sheet dismissal, preserving the artifact layer', () => {
		const artifact = vi.fn(() => true);
		const disposeArtifact = pushBackLayer(artifact);
		const artifactState = vi.mocked(history.pushState).mock.calls[0][0];
		const disposeSheet = pushBackLayer(() => true);
		const ordinaryNavigation = vi.fn();
		window.addEventListener('popstate', ordinaryNavigation);

		disposeSheet();
		expect(history.back).toHaveBeenCalledOnce();
		// This is the popstate the browser emits for the programmatic history.back,
		// not another call to the mocked history method.
		expect(pop(artifactState).stopImmediatePropagation).toHaveBeenCalledOnce();
		expect(artifact).not.toHaveBeenCalled();
		expect(ordinaryNavigation).not.toHaveBeenCalled();
		expect(backLayerDepth()).toBe(1);

		pop();
		expect(artifact).toHaveBeenCalledOnce();
		window.removeEventListener('popstate', ordinaryNavigation);
		disposeArtifact();
	});

	it('swallows direct artifact cleanup once, then lets ordinary navigation receive Back', () => {
		const disposeArtifact = pushBackLayer(() => true);
		const ordinaryNavigation = vi.fn();
		window.addEventListener('popstate', ordinaryNavigation);

		disposeArtifact();
		pop();
		expect(backLayerDepth()).toBe(0);
		expect(ordinaryNavigation).not.toHaveBeenCalled();

		const ordinaryPop = pop();
		expect(ordinaryPop.stopImmediatePropagation).not.toHaveBeenCalled();
		expect(ordinaryNavigation).toHaveBeenCalledOnce();
		window.removeEventListener('popstate', ordinaryNavigation);
	});

	it('matches a deferred cleanup to its lower state before a replacement layer can receive Back', () => {
		const artifact = vi.fn(() => true);
		pushBackLayer(artifact);
		const artifactState = vi.mocked(history.pushState).mock.calls[0][0];
		const disposeSheet = pushBackLayer(() => true);
		disposeSheet();
		const replacement = vi.fn(() => true);
		pushBackLayer(replacement);
		expect(history.pushState).toHaveBeenCalledTimes(2);

		// The direct sheet dismissal lands on the surviving artifact state even
		// though a thread switch pushed a replacement before this event arrived.
		expect(pop(artifactState).stopImmediatePropagation).toHaveBeenCalledOnce();
		expect(history.pushState).toHaveBeenCalledTimes(3);
		expect(replacement).not.toHaveBeenCalled();
		expect(backLayerDepth()).toBe(2);

		// The replacement's subsequent real Back is no longer masked.
		pop(artifactState);
		expect(replacement).toHaveBeenCalledOnce();
	});

	it('preserves a same-flush replacement while the old no-lower cleanup pop arrives', () => {
		const disposeOldArtifact = pushBackLayer(() => true);
		disposeOldArtifact();
		const replacement = vi.fn(() => true);
		pushBackLayer(replacement);
		expect(history.pushState).toHaveBeenCalledTimes(1);

		// This pop belongs to the disposed artifact's history.back(), not the
		// replacement. The replacement must remain the one live layer and only now
		// receive its own history entry at the browser's settled location.
		pop();
		expect(history.pushState).toHaveBeenCalledTimes(2);
		expect(replacement).not.toHaveBeenCalled();
		expect(backLayerDepth()).toBe(1);

		// Once cleanup was consumed, the user's next Back closes the replacement.
		pop();
		expect(replacement).toHaveBeenCalledOnce();
	});

	it('expires an undelivered no-lower cleanup before a later real Back', () => {
		vi.useFakeTimers();
		try {
			const disposeOldArtifact = pushBackLayer(() => true);
			disposeOldArtifact();
			const replacement = vi.fn(() => true);
			pushBackLayer(replacement);
			expect(history.pushState).toHaveBeenCalledTimes(1);

			vi.runOnlyPendingTimers();
			expect(history.pushState).toHaveBeenCalledTimes(2);
			pop();
			expect(replacement).toHaveBeenCalledOnce();
		} finally {
			vi.useRealTimers();
		}
	});

	it('does not rewind past a still-open layer when a buried one is disposed', () => {
		const buried = pushBackLayer(() => true);
		pushBackLayer(() => true);
		buried();
		expect(history.back).not.toHaveBeenCalled();
		expect(backLayerDepth()).toBe(1);
	});

	it('is inert once its layer was already consumed by a pop', () => {
		const dispose = pushBackLayer(() => true);
		pop();
		dispose();
		expect(history.back).not.toHaveBeenCalled();
	});
});
