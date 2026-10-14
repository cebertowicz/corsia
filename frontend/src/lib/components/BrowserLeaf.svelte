<script lang="ts">
	import { browser } from '$app/environment';
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';

	export let threadId: string;
	let canvas: HTMLCanvasElement;
	let socket: WebSocket | null = null;
	let controlled = false;
	let status = 'Agent is driving. Watch only';
	let url = '';
	let width = 0;
	let height = 0;
	let image: HTMLImageElement | null = null;

	function connect() {
		socket?.close();
		const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
		const next = new WebSocket(`${protocol}//${location.host}/api/browser/${threadId}/stream`);
		socket = next;
		next.onopen = () => next.send(JSON.stringify({ token: get(token) }));
		next.onmessage = (event) => drawFrame(event.data);
		next.onclose = () => { if (socket === next && !image) status = 'No browser yet'; };
	}

	function drawFrame(raw: string) {
		const frame = JSON.parse(raw);
		if (frame.type === 'browser_state') {
			status = 'No browser yet';
			return;
		}
		if (frame.type !== 'frame' || !frame.metadata) return;
		width = frame.metadata.deviceWidth;
		height = frame.metadata.deviceHeight;
		image = new Image();
		image.onload = () => redraw();
		image.src = `data:image/jpeg;base64,${frame.data}`;
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
		if (!controlled || !socket) return;
		const { x, y } = coordinates(event);
		const eventType = event.type === 'pointerdown' ? 'mousePressed' : event.type === 'pointerup' ? 'mouseReleased' : 'mouseMoved';
		socket.send(JSON.stringify({ type: 'input_mouse', eventType, x, y, button: 'left', clickCount: eventType === 'mousePressed' ? 1 : 0 }));
	}

	function sendWheel(event: WheelEvent) {
		if (!controlled || !socket) return;
		event.preventDefault();
		const { x, y } = coordinates(event as unknown as PointerEvent);
		socket.send(JSON.stringify({ type: 'input_mouse', eventType: 'mouseWheel', x, y, deltaX: event.deltaX, deltaY: event.deltaY }));
	}

	function sendKey(event: KeyboardEvent) {
		if (!controlled || !socket) return;
		event.preventDefault();
		socket.send(JSON.stringify({ type: 'input_keyboard', eventType: event.type === 'keydown' ? 'keyDown' : 'keyUp', key: event.key, code: event.code, text: event.key.length === 1 ? event.key : '' }));
	}

	async function toggleControl() {
		controlled = !controlled;
		status = controlled ? 'You have control. The agent waits' : 'Agent is driving. Watch only';
		await fetch(`/api/browser/${threadId}/control?has_control=${controlled}`, { method: 'POST', headers: { Authorization: `Bearer ${get(token)}` } });
	}

	async function go() {
		const response = await fetch(`/api/browser/${threadId}/open?url=${encodeURIComponent(url)}`, { method: 'POST', headers: { Authorization: `Bearer ${get(token)}` } });
		if (!response.ok) {
			status = 'Browser could not open this URL';
			return;
		}
		connect();
	}

	$: if (canvas) redraw();
	onMount(() => {
		const observer = new ResizeObserver(() => redraw());
		observer.observe(canvas);
		return () => {
			observer.disconnect();
			socket?.close();
		};
	});
	if (browser) connect();
</script>

<div class="browser-leaf">
	<div class="toolbar">
		<input aria-label="Browser address" bind:value={url} placeholder="http:// or https://" />
		<button type="button" onclick={go}>Go</button>
		<button type="button" onclick={toggleControl}>{controlled ? 'Hand back' : 'Take over'}</button>
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
