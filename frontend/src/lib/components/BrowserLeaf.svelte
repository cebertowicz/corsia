<script lang="ts">
	import { browser } from '$app/environment';
	import { openBrowserUrl } from '$lib/api/browser';
	import { token } from '$lib/stores/auth';
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
	let image = $state<HTMLImageElement | null>(null);
	let streaming = false;
	let opening = false;

	function connect() {
		socket?.close();
		const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
		const next = new WebSocket(`${protocol}//${location.host}/api/browser/${threadId}/stream`);
		socket = next;
		// A connecting socket counts as streaming, so browser_opened never races a second one.
		streaming = true;
		next.onopen = () => {
			next.send(JSON.stringify({ token: get(token) ?? '' }));
		};
		next.onmessage = (event) => drawMessage(event.data);
		next.onclose = () => {
			if (socket === next) streaming = false;
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
			status = hasControl ? 'You have control. The agent waits' : 'Agent is driving. Watch only';
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
		image = new Image();
		image.onload = redraw;
		image.src = `data:image/jpeg;base64,${message.data}`;
	}

	function redraw() {
		if (!canvas || !image || !width || !height) return;
		const bounds = canvas.getBoundingClientRect();
		const scale = Math.min(bounds.width / width, bounds.height / height);
		canvas.width = bounds.width * devicePixelRatio;
		canvas.height = bounds.height * devicePixelRatio;
		const context = canvas.getContext('2d');
		if (!context) return;
		context.scale(devicePixelRatio, devicePixelRatio);
		context.drawImage(image, (bounds.width - width * scale) / 2, (bounds.height - height * scale) / 2, width * scale, height * scale);
	}

	function coordinates(event: PointerEvent) {
		const bounds = canvas.getBoundingClientRect();
		return { x: (event.clientX - bounds.left) * width / bounds.width, y: (event.clientY - bounds.top) * height / bounds.height };
	}

	function sendMouse(event: PointerEvent) {
		if (!hasControl || socket?.readyState !== WebSocket.OPEN) return;
		const { x, y } = coordinates(event);
		const eventType = event.type === 'pointerdown' ? 'mousePressed' : event.type === 'pointerup' ? 'mouseReleased' : 'mouseMoved';
		socket.send(JSON.stringify({ type: 'input_mouse', eventType, x, y, button: 'left', clickCount: eventType === 'mousePressed' ? 1 : 0 }));
	}

	function sendWheel(event: WheelEvent) {
		if (!hasControl || socket?.readyState !== WebSocket.OPEN) return;
		event.preventDefault();
		const { x, y } = coordinates(event as unknown as PointerEvent);
		socket.send(JSON.stringify({ type: 'input_mouse', eventType: 'mouseWheel', x, y, deltaX: event.deltaX, deltaY: event.deltaY }));
	}

	function sendKey(event: KeyboardEvent) {
		if (!hasControl || socket?.readyState !== WebSocket.OPEN) return;
		event.preventDefault();
		socket.send(JSON.stringify({ type: 'input_keyboard', eventType: event.type === 'keydown' ? 'keyDown' : 'keyUp', key: event.key, code: event.code, text: event.key.length === 1 ? event.key : '' }));
	}

	function toggleControl() {
		if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: hasControl ? 'hand_back' : 'take_over' }));
	}

	async function go() {
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

	$effect(() => {
		if (!canvas) return;
		const observer = new ResizeObserver(redraw);
		observer.observe(canvas);
		redraw();
		return () => observer.disconnect();
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
	<div class="toolbar">
		<input aria-label="Browser address" bind:value={url} placeholder="http:// or https://" />
		<button type="button" onclick={go}>Go</button>
		<button type="button" onclick={toggleControl}>{hasControl ? 'Hand back' : 'Take over'}</button>
		<span>{status}</span>
	</div>
	<canvas bind:this={canvas} tabindex="0" aria-label="Shared browser" onpointerdown={sendMouse} onpointerup={sendMouse} onpointermove={sendMouse} onwheel={sendWheel} onkeydown={sendKey} onkeyup={sendKey}></canvas>
</div>

<style>
	.browser-leaf { flex: 1; min-height: 0; display: flex; flex-direction: column; background: var(--bg-primary); }
	.toolbar { display: flex; gap: .5rem; align-items: center; padding: .5rem; font-size: .8rem; }
	.toolbar input { flex: 1; }
	canvas { flex: 1; min-height: 0; width: 100%; outline: none; }
</style>
