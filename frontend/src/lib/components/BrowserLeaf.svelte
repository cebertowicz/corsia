<script lang="ts">
	import { browser } from '$app/environment';
	import { openBrowserUrl } from '$lib/api/browser';
	import { token } from '$lib/stores/auth';
	import { theme } from '$lib/stores/theme';
	import { AnimationFrameCoalescer, clampPoint, LatestFramePipeline, type PanePoint } from '$lib/browserPane';
	import { on } from '$lib/ws/events';
	import { get } from 'svelte/store';

	let { threadId }: { threadId: string } = $props();
	let canvas: HTMLCanvasElement;
	let socket: WebSocket | null = null;
	let hasControl = $state(false);
	let status = $state('Connecting to browser');
	let url = $state('');
	let width = $state(0);
	let height = $state(0);
	let streaming = $state(false);
	let opening = false;
	let fitTimer: ReturnType<typeof setTimeout> | undefined;
	let lastAppearance = '';
	let activePointer: number | null = null;
	let touchStart: { clientX: number; clientY: number; point: PanePoint } | null = null;
	let touchLast: { clientX: number; clientY: number; point: PanePoint } | null = null;
	let touchDragging = false;
	const framePipeline = new LatestFramePipeline(drawFrame);
	const mouseMoves = new AnimationFrameCoalescer(sendMouseMove);
	const touchWheels = new AnimationFrameCoalescer(sendTouchWheel);

	function connect() {
		socket?.close();
		const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
		const next = new WebSocket(`${protocol}//${location.host}/api/browser/${threadId}/stream`);
		lastAppearance = '';
		socket = next;
		// A connecting socket counts as streaming, so browser_opened never races a second one.
		streaming = true;
		next.onopen = () => {
			next.send(JSON.stringify({ token: get(token) ?? '' }));
			sendAppearance();
		};
		next.onmessage = (event) => drawMessage(event.data);
		next.onclose = () => {
			if (socket !== next) return;
			// The server releases control whenever this connection ends, so the pane must too.
			if (streaming) status = 'Browser stream ended. Reconnect to watch';
			streaming = false;
			hasControl = false;
		};
	}

	function drawMessage(raw: string) {
		let message: Record<string, unknown>;
		try {
			message = JSON.parse(raw) as Record<string, unknown>;
		} catch {
			return;
		}
		if (message.type === 'control' && typeof message.has_control === 'boolean') {
			hasControl = message.has_control;
			status = controlStatus();
			return;
		}
		if (message.type === 'navigate_result') {
			status = typeof message.error === 'string' ? `Browser could not open this URL: ${message.error}` : controlStatus();
			return;
		}
		if (message.type === 'browser_state') {
			streaming = false;
			if (message.state === 'no_browser_yet') status = 'No browser yet';
			else if (message.state === 'error') status = `Browser stream unavailable: ${message.reason ?? 'unknown error'}`;
			return;
		}
		if (message.type !== 'frame' || !message.metadata || typeof message.data !== 'string') return;
		const metadata = message.metadata as Record<string, unknown>;
		if (typeof metadata.deviceWidth !== 'number' || typeof metadata.deviceHeight !== 'number') return;
		const requested = lastAppearance ? JSON.parse(lastAppearance) as { width?: number; height?: number } : null;
		if (requested && (requested.width !== metadata.deviceWidth || requested.height !== metadata.deviceHeight)) {
			// A viewport match can race browser startup. Only remember a size after its frame confirms it.
			lastAppearance = '';
			clearTimeout(fitTimer);
			fitTimer = setTimeout(sendAppearance, 150);
		}
		width = metadata.deviceWidth;
		height = metadata.deviceHeight;
		framePipeline.offer({ data: message.data, width, height });
	}

	// Where the page sits in the canvas: drawing and input share it, so a click lands where it is drawn.
	function placement() {
		const bounds = canvas.getBoundingClientRect();
		const scale = Math.min(bounds.width / width, bounds.height / height);
		return { bounds, scale, left: (bounds.width - width * scale) / 2, top: (bounds.height - height * scale) / 2 };
	}

	function drawFrame(bitmap: ImageBitmap, frame: { width: number; height: number }) {
		if (!canvas || !frame.width || !frame.height) return;
		const { bounds, scale, left, top } = placement();
		const backingWidth = Math.round(bounds.width * devicePixelRatio);
		const backingHeight = Math.round(bounds.height * devicePixelRatio);
		// Changing a canvas backing store clears it; leave the current picture intact for ordinary frames.
		if (canvas.width !== backingWidth || canvas.height !== backingHeight) {
			canvas.width = backingWidth;
			canvas.height = backingHeight;
		}
		const context = canvas.getContext('2d');
		if (!context) return;
		context.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
		const density = Math.min(bitmap.width / frame.width, bitmap.height / frame.height);
		context.drawImage(bitmap, 0, 0, frame.width * density, frame.height * density, left, top, frame.width * scale, frame.height * scale);
	}

	function redraw() {
		framePipeline.redraw();
	}

	// The page takes the pane's size, pixel density and CORSIA theme: it fills the pane sharply, never glaring.
	function sendAppearance() {
		if (!canvas || socket?.readyState !== WebSocket.OPEN) return;
		const bounds = canvas.getBoundingClientRect();
		if (bounds.width < 1 || bounds.height < 1) return;
		const appearance = { type: 'appearance', width: Math.round(bounds.width), height: Math.round(bounds.height), scale: devicePixelRatio, scheme: get(theme) };
		const key = JSON.stringify(appearance);
		if (key === lastAppearance) return;
		lastAppearance = key;
		socket.send(key);
	}

	function coordinates(event: PointerEvent | WheelEvent, clamp = false) {
		const { bounds, scale, left, top } = placement();
		const point = { x: (event.clientX - bounds.left - left) / scale, y: (event.clientY - bounds.top - top) / scale };
		return clamp ? clampPoint(point, width, height) : point.x >= 0 && point.y >= 0 && point.x <= width && point.y <= height ? point : null;
	}

	function sendInput(eventType: 'mouseMoved' | 'mousePressed' | 'mouseReleased', point: PanePoint, button: 'left' | 'none', clickCount: number) {
		if (socket?.readyState !== WebSocket.OPEN) return;
		socket.send(JSON.stringify({ type: 'input_mouse', eventType, ...point, button, clickCount }));
	}

	function sendMouseMove(point: PanePoint) {
		if (hasControl) sendInput('mouseMoved', point, 'none', 0);
	}

	function sendTouchWheel(value: { point: PanePoint; deltaX: number; deltaY: number }) {
		if (hasControl && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input_mouse', eventType: 'mouseWheel', ...value }));
	}

	function sendMouse(event: PointerEvent) {
		if (!hasControl || socket?.readyState !== WebSocket.OPEN || !width || !height) return;
		if (event.pointerType === 'touch') {
			handleTouch(event);
			return;
		}
		if (event.type === 'pointerdown') {
			canvas.focus();
			activePointer = event.pointerId;
			canvas.setPointerCapture(event.pointerId);
			const point = coordinates(event, true);
			if (!point) return;
			// Move before pressing because Chrome may use the last cursor position for a press.
			sendInput('mouseMoved', point, 'none', 0);
			sendInput('mousePressed', point, 'left', 1);
			return;
		}
		const point = coordinates(event, event.type === 'pointerup' || event.type === 'pointercancel');
		if (!point) return;
		if (event.type === 'pointermove') mouseMoves.push(point);
		else {
			mouseMoves.cancel();
			sendInput('mouseReleased', point, 'left', 1);
			if (activePointer === event.pointerId && canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
			activePointer = null;
		}
	}

	function handleTouch(event: PointerEvent) {
		const point = coordinates(event, true);
		if (!point) return;
		if (event.type === 'pointerdown') {
			canvas.focus();
			activePointer = event.pointerId;
			canvas.setPointerCapture(event.pointerId);
			touchStart = { clientX: event.clientX, clientY: event.clientY, point };
			touchLast = touchStart;
			touchDragging = false;
			return;
		}
		if (!touchStart || !touchLast) return;
		if (event.type === 'pointermove') {
			event.preventDefault();
			const distance = Math.hypot(event.clientX - touchStart.clientX, event.clientY - touchStart.clientY);
			if (distance > 8) touchDragging = true;
			if (touchDragging) {
				const { scale } = placement();
				touchWheels.push({ point, deltaX: (touchLast.clientX - event.clientX) / scale, deltaY: (touchLast.clientY - event.clientY) / scale });
			}
			touchLast = { clientX: event.clientX, clientY: event.clientY, point };
			return;
		}
		if (!touchDragging && event.type === 'pointerup') {
			sendInput('mouseMoved', touchStart.point, 'none', 0);
			sendInput('mousePressed', touchStart.point, 'left', 1);
			sendInput('mouseReleased', point, 'left', 1);
		}
		touchWheels.cancel();
		if (activePointer === event.pointerId && canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
		activePointer = null;
		touchStart = null;
		touchLast = null;
	}

	function sendWheel(event: WheelEvent) {
		if (!hasControl || socket?.readyState !== WebSocket.OPEN) return;
		event.preventDefault();
		const point = coordinates(event);
		if (!point) return;
		socket.send(JSON.stringify({ type: 'input_mouse', eventType: 'mouseWheel', ...point, deltaX: event.deltaX, deltaY: event.deltaY }));
	}

	function sendKey(event: KeyboardEvent) {
		if (!hasControl || socket?.readyState !== WebSocket.OPEN) return;
		event.preventDefault();
		socket.send(JSON.stringify({ type: 'input_keyboard', eventType: event.type === 'keydown' ? 'keyDown' : 'keyUp', key: event.key, code: event.code, text: keyText(event) }));
	}

	// Chrome types only keys that carry text; Enter must carry a return to submit a form.
	function keyText(event: KeyboardEvent) {
		if (event.type !== 'keydown' || event.metaKey || event.ctrlKey) return '';
		return event.key === 'Enter' ? '\r' : event.key.length === 1 ? event.key : '';
	}

	function toggleControl() {
		if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: hasControl ? 'hand_back' : 'take_over' }));
	}

	function controlStatus() {
		return hasControl ? 'You have control. The agent waits' : 'Agent is driving. Watch only';
	}

	async function go() {
		// An open browser navigates only through the stream that holds control.
		if (streaming) {
			if (!hasControl || socket?.readyState !== WebSocket.OPEN) return;
			status = 'Opening';
			socket.send(JSON.stringify({ type: 'navigate', url }));
			return;
		}
		opening = true;
		try {
			await openBrowserUrl(threadId, url, get(token) ?? '');
			connect();
		} catch (error) {
			status = `Browser could not open this URL: ${error instanceof Error ? error.message : 'request failed'}`;
		} finally {
			opening = false;
		}
	}

	function history(action: 'back' | 'forward' | 'reload') {
		if (!hasControl || socket?.readyState !== WebSocket.OPEN) return;
		socket.send(JSON.stringify({ type: 'navigate', action }));
	}

	function submitAddress(event: SubmitEvent) {
		event.preventDefault();
		void go();
	}

	$effect(() => {
		if (!canvas) return;
		const observer = new ResizeObserver(() => {
			redraw();
			clearTimeout(fitTimer);
			fitTimer = setTimeout(sendAppearance, 150);
		});
		const unsubscribeTheme = theme.subscribe(() => sendAppearance());
		observer.observe(canvas);
		redraw();
		return () => {
			observer.disconnect();
			unsubscribeTheme();
			clearTimeout(fitTimer);
			mouseMoves.cancel();
			touchWheels.cancel();
			framePipeline.dispose();
		};
	});

	$effect(() => {
		if (!browser) return;
		connect();
		const unsubscribe = on('browser_opened', (data: { thread_id?: string }) => {
			if (data?.thread_id === threadId && !streaming && !opening) connect();
		});
		return () => {
			unsubscribe();
			socket?.close();
		};
	});
</script>

<div class="browser-leaf">
	<form class="toolbar" onsubmit={submitAddress}>
		<button type="button" class="icon" aria-label="Back" title="Back" onclick={() => history('back')} disabled={!streaming || !hasControl}>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
		</button>
		<button type="button" class="icon" aria-label="Forward" title="Forward" onclick={() => history('forward')} disabled={!streaming || !hasControl}>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
		</button>
		<button type="button" class="icon" aria-label="Reload" title="Reload" onclick={() => history('reload')} disabled={!streaming || !hasControl}>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" /></svg>
		</button>
		<input aria-label="Browser address" bind:value={url} placeholder="http:// or https://" />
		<button type="submit" disabled={streaming && !hasControl}>Go</button>
		{#if streaming}
			<button type="button" onclick={toggleControl}>{hasControl ? 'Hand back' : 'Take over'}</button>
		{:else}
			<button type="button" onclick={connect}>Reconnect</button>
		{/if}
		<span>{status}</span>
	</form>
	<canvas bind:this={canvas} tabindex="0" aria-label="Shared browser" onpointerdown={sendMouse} onpointerup={sendMouse} onpointercancel={sendMouse} onpointermove={sendMouse} onwheel={sendWheel} onkeydown={sendKey} onkeyup={sendKey}></canvas>
</div>

<style>
	.browser-leaf { flex: 1; min-height: 0; display: flex; flex-direction: column; background: var(--bg-primary); }
	.toolbar { display: flex; gap: .5rem; align-items: center; padding: .5rem; font-size: .8rem; }
	.toolbar input { flex: 1; min-width: 0; }
	.toolbar .icon { display: inline-flex; padding: 2px; border: 0; background: none; color: var(--text-secondary); cursor: pointer; }
	.toolbar .icon:disabled { color: var(--text-tertiary); cursor: default; }
	.toolbar .icon svg { width: 16px; height: 16px; }
	canvas { flex: 1; min-height: 0; width: 100%; outline: none; touch-action: none; }
</style>
