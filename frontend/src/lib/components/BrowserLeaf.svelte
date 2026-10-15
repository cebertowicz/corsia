<script lang="ts">
	import { browser } from '$app/environment';
	import { openBrowserUrl } from '$lib/api/browser';
	import { token } from '$lib/stores/auth';
	import { theme } from '$lib/stores/theme';
	import { clampPoint } from '$lib/browserPane';
	import { agentColor } from '$lib/docPresence';
	import { BrowserPaneInput } from '$lib/browserPaneInput';
	import { browserAddressLabel, browserTabLabel } from '$lib/browserAddress';
	import { setBrowserPaneLabel } from '$lib/stores/artifactWorkspace';
	import AvatarStack from '$lib/components/ui/AvatarStack.svelte';
	import { on } from '$lib/ws/events';
	import { get } from 'svelte/store';
	import { untrack } from 'svelte';

	let { threadId }: { threadId: string } = $props();
	let canvas: HTMLCanvasElement;
	let socket: WebSocket | null = null;
	let passwordPage = $state(false);
	let keyboardOpen = $state(false);
	let addressFocused = $state(false);
	let status = $state('Connecting to browser');
	const presence = $derived([{ id: threadId, name: 'Agent', color: agentColor(threadId), detail: 'Agent is in this page' }]);
	let url = $state('');
	let addressText = $state('');
	const shownAddress = $derived(addressFocused ? addressText : browserAddressLabel(url));
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
		if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
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
		// A stream is usable only after the server confirms an existing browser page.
		streaming = false;
		next.onopen = () => {
			next.send(JSON.stringify({ token: get(token) ?? '' }));
			sendAppearance();
			// A newly opened socket can beat the canvas's first layout; send again once it is laid out.
			requestAnimationFrame(() => sendAppearance());
		};
		next.onmessage = (event) => { if (socket === next) drawMessage(event.data); };
		next.onclose = () => {
			if (socket !== next) return;
			// The server releases control whenever this connection ends, so the pane must too.
			if (streaming) status = 'Browser stream ended. Reconnect to watch';
			streaming = false;
			resetPointer();
		};
	}

	// A stream that reported no browser, or an HTTP open, has no navigate_result to say the page is ready.
	function pageConfirmed() {
		if (!streaming) status = 'Ready';
		streaming = true;
	}

	function applyPageMetadata(nextUrl: unknown, nextTitle: unknown) {
		if (typeof nextUrl === 'string') url = nextUrl;
		if (url) setBrowserPaneLabel(threadId, browserTabLabel(typeof nextTitle === 'string' ? nextTitle : null, url));
	}

	function drawMessage(raw: string) {
		let message: Record<string, unknown>;
		try {
			message = JSON.parse(raw) as Record<string, unknown>;
		} catch {
			return;
		}
		if (message.type === 'browser_password_page' && typeof message.visible === 'boolean') {
			passwordPage = message.visible;
			if (!passwordPage) status = 'Agent can see the page again';
			return;
		}
		if (message.type === 'browser_metadata') {
			pageConfirmed();
			applyPageMetadata(message.url, message.title);
			return;
		}
		if (message.type === 'navigate_result') {
			streaming = true;
			if (typeof message.error === 'string') status = `Browser could not open this URL: ${message.error}`;
			else {
				applyPageMetadata(message.url, message.title);
				status = 'Ready';
			}
			return;
		}
		if (message.type === 'browser_state') {
			streaming = false;
			if (message.state === 'no_browser_yet') status = 'No browser yet';
			else if (message.state === 'error') status = `Browser stream unavailable: ${message.reason ?? 'unknown error'}`;
			return;
		}
		if (message.type !== 'frame' || !message.metadata || typeof message.data !== 'string') return;
		pageConfirmed();
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
		if (socket?.readyState !== WebSocket.OPEN || !width || !height) return;
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
		if (socket?.readyState !== WebSocket.OPEN) return;
		event.preventDefault();
		const point = coordinates(event);
		if (!point) return;
		socket.send(JSON.stringify({ type: 'input_mouse', eventType: 'mouseWheel', ...point, deltaX: event.deltaX, deltaY: event.deltaY, deltaMode: event.deltaMode }));
	}

	function sendKey(event: KeyboardEvent) {
		if (socket?.readyState !== WebSocket.OPEN) return;
		event.preventDefault();
		socket.send(JSON.stringify({
			type: 'input_keyboard', eventType: event.type === 'keydown' ? 'keyDown' : 'keyUp',
			key: event.key, code: event.code, keyCode: event.keyCode, text: keyText(event), modifiers: keyModifiers(event)
		}));
	}

	// Chrome's CDP modifier bits preserve shortcuts that the person performs in the shared page.
	function keyModifiers(event: KeyboardEvent) {
		return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
	}

	// Chrome types only keys that carry text; Enter must carry a return to submit a form.
	function keyText(event: KeyboardEvent) {
		if (event.type !== 'keydown' || event.metaKey || event.ctrlKey) return '';
		return event.key === 'Enter' ? '\r' : event.key.length === 1 ? event.key : '';
	}

	function controlStatus() {
		return 'Ready';
	}

	async function go(targetUrl = url) {
		// An open browser navigates only through the stream that holds control.
		if (streaming && socket?.readyState === WebSocket.OPEN) {
			status = 'Opening';
			socket.send(JSON.stringify({ type: 'navigate', url: targetUrl }));
			return;
		}
		opening = true;
		try {
			await openBrowserUrl(threadId, targetUrl, get(token) ?? '');
			connect();
		} catch (error) {
			status = `Browser could not open this URL: ${error instanceof Error ? error.message : 'request failed'}`;
		} finally {
			opening = false;
		}
	}

	function history(action: 'back' | 'forward' | 'reload') {
		if (socket?.readyState !== WebSocket.OPEN) return;
		socket.send(JSON.stringify({ type: 'navigate', action }));
	}

	function submitAddress(event: SubmitEvent) {
		event.preventDefault();
		const targetUrl = addressText;
		url = targetUrl;
		addressFocused = false;
		void go(targetUrl);
	}

	function focusAddress(event: FocusEvent) {
		addressText = url;
		addressFocused = true;
		// Swap in the full URL now and select it, as a browser does. Left to the next render,
		// the swap would land after a selection made on focus and append the person's text to it.
		const input = event.currentTarget as HTMLInputElement;
		input.value = url;
		input.select();
	}

	function editAddress(event: Event) {
		addressText = (event.currentTarget as HTMLInputElement).value;
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

	// The host passes threadId from its active tab, so reading the prop tracks that tab object;
	// any tab change (this pane relabelling itself included) would otherwise reconnect the stream.
	const streamThreadId = $derived(threadId);

	$effect(() => {
		if (!browser || !streamThreadId) return;
		const viewport = window.visualViewport;
		const updateKeyboard = () => { keyboardOpen = !!viewport && window.innerHeight - viewport.height > 160; };
		updateKeyboard();
		viewport?.addEventListener('resize', updateKeyboard);
		untrack(connect);
		const unsubscribe = on('browser_opened', (data: { thread_id?: string }) => {
			if (data?.thread_id === threadId && !streaming && !opening && (!socket || socket.readyState === WebSocket.CLOSED)) connect();
		});
		return () => {
			unsubscribe();
			viewport?.removeEventListener('resize', updateKeyboard);
			socket?.close();
		};
	});
</script>

<div class="browser-leaf">
	<form class="toolbar" onsubmit={submitAddress}>
		<button type="button" class="icon" aria-label="Back" title="Back" onclick={() => history('back')} disabled={!streaming}>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
		</button>
		<button type="button" class="icon" aria-label="Forward" title="Forward" onclick={() => history('forward')} disabled={!streaming}>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
		</button>
		<button type="button" class="icon" aria-label="Reload" title="Reload" onclick={() => history('reload')} disabled={!streaming}>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6" /></svg>
		</button>
		<div class="agent-seam"><AvatarStack people={presence} max={3} /></div>
		<input aria-label="Browser address" value={shownAddress} placeholder="http:// or https://" onfocus={focusAddress} onblur={() => addressFocused = false} oninput={editAddress} />
		<button type="submit" disabled={false}>Go</button>
		{#if streaming}
			<span>Shared with the agent</span>
		{:else}
			<button type="button" onclick={connect}>Reconnect</button>
		{/if}
		<span>{status}</span>
	</form>
	<div class:agent-present={!passwordPage} class:keyboard-open={keyboardOpen} class="browser-page" style:--presence-color={agentColor(threadId)}>
		<span class="agent-field-marker" aria-label="Agent is active in this page">Agent</span>
		<canvas bind:this={canvas} tabindex="0" aria-label="Shared browser" onpointerdown={sendMouse} onpointerup={sendMouse} onpointercancel={sendMouse} onpointermove={sendMouse} onwheel={sendWheel} onkeydown={sendKey} onkeyup={sendKey}></canvas>
		{#if passwordPage}<div class="password-notice" class:top={keyboardOpen} role="status"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 11V8a5 5 0 0 1 10 0v3M6 11h12v10H6z" /></svg>Enter your password to continue</div>{/if}
	</div>
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
	.agent-seam { display: inline-flex; }
	.browser-page { position: relative; display: flex; flex: 1; min-height: 0; }
	.browser-page::after { content: ''; position: absolute; inset: 0; border: 2px solid var(--presence-color, var(--accent)); opacity: 0; pointer-events: none; transition: opacity 160ms ease; }
	.browser-page.agent-present::after { opacity: 1; }
	.agent-field-marker { position: absolute; z-index: 1; top: 10px; right: 10px; padding: 3px 7px; border: 1px solid var(--presence-color, var(--accent)); border-radius: var(--radius-pill); color: var(--presence-color, var(--accent)); background: var(--bg-primary); font-size: var(--text-caption); opacity: 0; pointer-events: none; transition: opacity 160ms ease; }
	.browser-page.agent-present .agent-field-marker { opacity: 1; }
	.password-notice { position: absolute; z-index: 1; bottom: 16px; left: 50%; display: inline-flex; gap: 8px; width: max-content; height: 44px; align-items: center; padding: 0 16px; border: 1px solid color-mix(in srgb, var(--border) 72%, transparent); border-radius: var(--radius-pill); background: var(--glass-regular-thick); box-shadow: var(--glass-elevation-1), var(--glass-rim); backdrop-filter: var(--glass-blur); transform: translateX(-50%); }
	.password-notice.top { top: 10px; bottom: auto; }
	.password-notice svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2; }
	canvas { flex: 1; min-height: 0; width: 100%; outline: none; touch-action: none; }
	@media (prefers-reduced-motion: reduce) { .browser-page::after, .agent-field-marker { transition: none; } }
</style>
