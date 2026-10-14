<script lang="ts">
	import { browser } from '$app/environment';
	import { openBrowserUrl } from '$lib/api/browser';
	import { token } from '$lib/stores/auth';
	import { theme } from '$lib/stores/theme';
	import { clampPoint } from '$lib/browserPane';
	import { BrowserPaneInput } from '$lib/browserPaneInput';
	import { on } from '$lib/ws/events';
	import { get } from 'svelte/store';
	import { untrack } from 'svelte';

	let { threadId }: { threadId: string } = $props();
	let canvas: HTMLCanvasElement;
	let socket: WebSocket | null = null;
	let hasControl = $state(false);
	let status = $state('Connecting to browser');
	let url = $state('');
	let width = $state(0);
	let height = $state(0);
	let image = $state<HTMLImageElement | null>(null);
	let streaming = $state(false);
	let opening = false;
	let fitTimer: ReturnType<typeof setTimeout> | undefined;
	let lastAppearance = '';
	let activePointer: number | null = null;
	let decodeGeneration = 0;
	let decodeWarned = false;
	let drawnArea = '';
	const paneInput = new BrowserPaneInput(message => {
		if (hasControl && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
	});

	function resetPointer() {
		paneInput.reset();
		if (activePointer !== null && canvas?.hasPointerCapture(activePointer)) canvas.releasePointerCapture(activePointer);
		activePointer = null;
	}

	function connect() {
		resetPointer();
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
		next.onmessage = (event) => { if (socket === next) drawMessage(event.data); };
		next.onclose = () => {
			if (socket !== next) return;
			// The server releases control whenever this connection ends, so the pane must too.
			if (streaming) status = 'Browser stream ended. Reconnect to watch';
			streaming = false;
			hasControl = false;
			resetPointer();
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
			if (!hasControl) resetPointer();
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
		width = metadata.deviceWidth;
		height = metadata.deviceHeight;
		const generation = ++decodeGeneration;
		const next = new Image();
		next.src = `data:image/jpeg;base64,${message.data}`;
		void next.decode().then(() => {
			if (generation !== decodeGeneration) return;
			image = next;
			redraw();
		}, (error) => {
			// An undecodable frame keeps the last good picture; a drawing failure is not caught here.
			if (!decodeWarned) console.warn('Browser pane could not decode a frame', error);
			decodeWarned = true;
		});
	}

	// Where the page sits in the canvas: drawing and input share it, so a click lands where it is drawn.
	function placement() {
		const bounds = canvas.getBoundingClientRect();
		const scale = Math.min(bounds.width / width, bounds.height / height);
		return { bounds, scale, left: (bounds.width - width * scale) / 2, top: (bounds.height - height * scale) / 2 };
	}

	function redraw() {
		if (!canvas || !image || !width || !height) return;
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
		// A new page size moves the letterbox even when the pane keeps its size: clear the whole backing
		// store so no strip of the earlier picture survives beside the new one.
		const area = `${left},${top},${width * scale},${height * scale}`;
		if (area !== drawnArea) {
			context.setTransform(1, 0, 0, 1, 0, 0);
			context.clearRect(0, 0, canvas.width, canvas.height);
			drawnArea = area;
		}
		context.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
		// Chrome can keep capturing at an older, smaller size: the frame then holds the page shrunk into its
		// top-left corner with blank space beside it, so draw only the page's part of the frame.
		// Frame pixels per page pixel; above 1 on a high-density pane.
		const density = Math.min(image.naturalWidth / width, image.naturalHeight / height);
		context.drawImage(image, 0, 0, width * density, height * density, left, top, width * scale, height * scale);
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

	function sendMouse(event: PointerEvent) {
		if (!hasControl || socket?.readyState !== WebSocket.OPEN || !width || !height) return;
		if (!event.isPrimary) return;
		const point = coordinates(event, event.type !== 'pointerdown' && (event.type !== 'pointermove' || activePointer !== null));
		if (!point) return;
		if (event.pointerType === 'touch') event.preventDefault();
		if (event.type === 'pointerdown') {
			canvas.focus();
			activePointer = event.pointerId;
			canvas.setPointerCapture(event.pointerId);
			// Move before pressing because Chrome may use the last cursor position for a press.
			paneInput.press(event, point);
		} else if (event.type === 'pointermove') {
			paneInput.move(event, point, placement().scale);
		} else {
			paneInput.release(event, point, event.type === 'pointercancel');
			if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
			activePointer = null;
		}
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
		// Untracked, so a new frame does not rebuild the observer and cancel a pending fit or pointer move.
		untrack(redraw);
		return () => {
			observer.disconnect();
			unsubscribeTheme();
			clearTimeout(fitTimer);
			paneInput.dispose();
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
	.toolbar { display: flex; flex-shrink: 0; gap: .5rem; align-items: center; padding: .5rem; font-size: .8rem; }
	.toolbar span { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.toolbar button { flex-shrink: 0; white-space: nowrap; }
	.toolbar input { flex: 1; min-width: 0; }
	.toolbar .icon { display: inline-flex; padding: 2px; border: 0; background: none; color: var(--text-secondary); cursor: pointer; }
	.toolbar .icon:disabled { color: var(--text-tertiary); cursor: default; }
	.toolbar .icon svg { width: 16px; height: 16px; }
	canvas { flex: 1; min-height: 0; width: 100%; outline: none; touch-action: none; }
</style>
