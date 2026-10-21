<script lang="ts">
	import { browser } from '$app/environment';
	import { openBrowserUrl } from '$lib/api/browser';
	import { token } from '$lib/stores/auth';
	import { theme } from '$lib/stores/theme';
	import { clampPoint, drawShifted, FrameScrollShift } from '$lib/browserPane';
	import { agentColor } from '$lib/docPresence';
	import { BrowserPaneInput, keyModifiers, keyText, wheelPixels } from '$lib/browserPaneInput';
	import { browserTabLabel } from '$lib/browserAddress';
	import { BrowserAgentPresence, markerPlace, type MarkerPlace } from '$lib/browserAgentPresence.svelte';
	import { setBrowserPaneLabel } from '$lib/stores/artifactWorkspace';
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import BrowserToolbar from '$lib/components/BrowserToolbar.svelte';
	import BrowserPageStatus from '$lib/components/BrowserPageStatus.svelte';
	import { threads } from '$lib/stores/threads';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { on } from '$lib/ws/events';
	import { fade } from 'svelte/transition';
	import { get } from 'svelte/store';
	import { untrack } from 'svelte';

	let { threadId }: { threadId: string } = $props();
	let canvas: HTMLCanvasElement;
	let socket: WebSocket | null = null;
	let passwordPage = $state(false);
	let keyboardOpen = $state(false);
	let status = $state('');
	let ended = $state(false);
	let announcement = $state('');
	const agent = new BrowserAgentPresence();
	// Kept after the mark clears, so the marker fades out where it was.
	let marker = $state<MarkerPlace | null>(null);
	// A refused agent is not in a password page, so its marks go however recently it acted.
	const agentPresent = $derived(agent.active && !passwordPage);
	const markerShown = $derived(agentPresent && agent.mark !== null);
	const agentName = $derived($threads.find((thread) => thread.id === threadId)?.title?.trim() || 'Agent');
	const presence = $derived([{ id: threadId, name: agentName, color: agentColor(threadId), detail: 'In this page' }]);
	let url = $state('');
	// The page's registrable domain, named by the backend from the Public Suffix List.
	let domain = $state<string | null>(null);
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
	const frameScroll = new FrameScrollShift(() => redraw());
	const paneInput = new BrowserPaneInput(message => {
		if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
	}, delta => {
		frameScroll.shift(delta);
		redraw();
	});

	function resetPointer() {
		paneInput.reset();
		if (activePointer !== null && canvas?.hasPointerCapture(activePointer)) canvas.releasePointerCapture(activePointer);
		activePointer = null;
	}

	function connect() {
		resetPointer();
		socket?.close();
		ended = false;
		if (status.startsWith('Browser stream')) status = '';
		// The server tells a new stream how recently the agent acted.
		agent.reset();
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
			// Input and navigation travel only over this connection, so an ended one leaves the pane offering a reconnect.
			if (streaming) {
				status = 'Browser stream ended';
				ended = true;
			}
			streaming = false;
			resetPointer();
		};
	}

	// A stream that reported no browser, or an HTTP open, has no navigate_result to say the page is ready.
	function pageConfirmed() {
		if (!streaming || status === 'Opening') status = '';
		streaming = true;
	}

	function applyPageMetadata(nextUrl: unknown, nextTitle: unknown, nextDomain: unknown) {
		// A marked field belongs to the page it was written in.
		if (typeof nextUrl === 'string' && nextUrl !== url) agent.clearMark();
		if (typeof nextUrl === 'string') {
			url = nextUrl;
			domain = typeof nextDomain === 'string' ? nextDomain : null;
		}
		if (url) setBrowserPaneLabel(threadId, browserTabLabel(typeof nextTitle === 'string' ? nextTitle : null, url, domain));
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
			// Emptied on each password page, so the next one announces again.
			announcement = passwordPage ? '' : 'Agent can see the page again';
			if (passwordPage) agent.clearMark();
			return;
		}
		if (message.type === 'browser_agent') {
			agent.acted(message);
			placeMarker();
			return;
		}
		if (message.type === 'browser_metadata') {
			pageConfirmed();
			applyPageMetadata(message.url, message.title, message.domain);
			return;
		}
		if (message.type === 'navigate_result') {
			streaming = true;
			if (typeof message.error === 'string') status = `Browser could not open this URL: ${message.error}`;
			else {
				applyPageMetadata(message.url, message.title, message.domain);
				status = '';
			}
			return;
		}
		if (message.type === 'browser_state') {
			streaming = false;
			if (message.state === 'no_browser_yet') status = 'No browser yet';
			else if (message.state === 'error') {
				status = `Browser stream unavailable: ${message.reason ?? 'unknown error'}`;
				ended = true;
			}
			return;
		}
		if (message.type !== 'frame' || !message.metadata || typeof message.data !== 'string') return;
		pageConfirmed();
		const metadata = message.metadata as Record<string, unknown>;
		if (typeof metadata.deviceWidth !== 'number' || typeof metadata.deviceHeight !== 'number') return;
		const frameWidth = metadata.deviceWidth;
		const frameHeight = metadata.deviceHeight;
		const generation = ++decodeGeneration;
		const next = new Image();
		next.src = `data:image/jpeg;base64,${message.data}`;
		void next.decode().then(() => {
			if (generation !== decodeGeneration) return;
			frameScroll.replace(metadata);
			width = frameWidth;
			height = frameHeight;
			image = next;
			redraw();
		}, (error) => {
			// An undecodable frame keeps the last good picture; a drawing failure is not caught here.
			if (!decodeWarned) console.warn('Browser pane could not decode a frame', error);
			decodeWarned = true;
		});
	}

	// The marker maps the field's page pixels with the same placement as drawing and input.
	function placeMarker() {
		if (!agent.mark || !canvas || !width || !height) return;
		const { bounds, scale, left, top } = placement();
		marker = markerPlace(agent.mark, { left: canvas.offsetLeft + left, top: canvas.offsetTop + top, scale, right: canvas.offsetLeft + bounds.width });
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
		// Predicted scroll also moves the visible rectangle, so it needs the same clear.
		context.setTransform(1, 0, 0, 1, 0, 0);
		context.clearRect(0, 0, canvas.width, canvas.height);
		context.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
		// Chrome can keep capturing at an older, smaller size: the frame then holds the page shrunk into its
		// top-left corner with blank space beside it, so draw only the page's part of the frame.
		// Frame pixels per page pixel; above 1 on a high-density pane.
		const density = Math.min(image.naturalWidth / width, image.naturalHeight / height);
		drawShifted(context, image, width * density, height * density, left, top, width * scale, height * scale, frameScroll.translation, scale);
		placeMarker();
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
			// A touch drag scrolls the page, and the marker cannot follow its field there.
			if (event.pointerType === 'touch' && activePointer !== null) agent.clearMark();
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
		// The person's scroll moves the agent's field, and the marker cannot follow it there.
		agent.clearMark();
		// CDP takes wheel deltas in CSS pixels, so a line or page wheel is converted first.
		paneInput.wheel(point, wheelPixels(event, height));
	}

	function sendKey(event: KeyboardEvent) {
		if (socket?.readyState !== WebSocket.OPEN) return;
		event.preventDefault();
		paneInput.flush();
		socket.send(JSON.stringify({
			type: 'input_keyboard', eventType: event.type === 'keydown' ? 'keyDown' : 'keyUp',
			key: event.key, code: event.code, keyCode: event.keyCode, text: keyText(event), modifiers: keyModifiers(event)
		}));
	}

	async function go(targetUrl = url) {
		// An open browser navigates through the pane's stream, the same page target the agent acts on.
		if (streaming && socket?.readyState === WebSocket.OPEN) {
			status = 'Opening';
			socket.send(JSON.stringify({ type: 'navigate', url: targetUrl }));
			return;
		}
		opening = true;
		status = 'Opening';
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

	function navigate(targetUrl: string) {
		url = targetUrl;
		// The typed address has no backend-named domain until its page reports one.
		domain = null;
		void go(targetUrl);
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
			agent.reset();
			paneInput.dispose();
			frameScroll.dispose();
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
	<BrowserToolbar {url} {domain} {streaming} {announcement} people={presence} {agentPresent}
		onnavigate={navigate} onhistory={history} />
	<div class:agent-present={agentPresent} class:keyboard-open={keyboardOpen} class:ended class="browser-page" style:--presence-color={agentColor(threadId)}>
		{#if marker}
			<span class="agent-field-marker" class:shown={markerShown} class:above={marker.above} style:left="{marker.left}px" style:top="{marker.top}px"
				role="img" aria-label="{agentName} is filling this field" aria-hidden={!markerShown} title="{agentName} is filling this field">
				<Avatar name={agentName} color={agentColor(threadId)} hidden />
			</span>
		{/if}
		<canvas bind:this={canvas} tabindex="0" aria-label="Shared browser" onpointerdown={sendMouse} onpointerup={sendMouse} onpointercancel={sendMouse} onpointermove={sendMouse} onwheel={sendWheel} onkeydown={sendKey} onkeyup={sendKey}></canvas>
		<BrowserPageStatus {status} {streaming} {ended} onreconnect={connect} onretry={() => void go()} />
		{#if passwordPage}<div class="password-notice" class:top={keyboardOpen} role="status" transition:fade={{ duration: $prefersReducedMotion ? 0 : 160 }}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 11V8a5 5 0 0 1 10 0v3M6 11h12v10H6z" /></svg>Enter your password to continue</div>{/if}
	</div>
</div>

<style>
	.browser-leaf { flex: 1; min-height: 0; display: flex; flex-direction: column; background: var(--bg-primary); }
	.browser-page { position: relative; display: flex; flex: 1; min-height: 0; overflow: hidden; }
	.browser-page::after { content: ''; position: absolute; inset: 0; border: 2px solid var(--presence-color, var(--accent)); opacity: 0; pointer-events: none; transition: opacity 160ms ease; }
	.browser-page.agent-present::after { opacity: 1; }
	.agent-field-marker { position: absolute; z-index: 1; display: inline-flex; transform: translateY(-50%); opacity: 0; visibility: hidden; transition: opacity 160ms ease, visibility 0s linear 160ms; }
	.agent-field-marker.above { transform: translate(-100%, -100%); }
	.agent-field-marker.shown { opacity: 1; visibility: visible; transition: opacity 160ms ease; }
	.agent-field-marker :global(.avatar) { box-shadow: 0 0 0 2px var(--bg-primary); }
	.password-notice { position: absolute; z-index: 1; bottom: 16px; left: 50%; display: inline-flex; gap: 8px; width: max-content; height: 44px; align-items: center; padding: 0 16px; border: 1px solid color-mix(in srgb, var(--border) 72%, transparent); border-radius: var(--radius-pill); background: var(--glass-regular-thick); box-shadow: var(--glass-elevation-1), var(--glass-rim); backdrop-filter: var(--glass-blur); transform: translateX(-50%); }
	.password-notice.top { top: 10px; bottom: auto; }
	.password-notice svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2; }
	canvas { flex: 1; min-height: 0; width: 100%; outline: none; touch-action: none; transition: opacity 160ms ease; }
	.browser-page.ended canvas { opacity: 0.4; }
	@media (prefers-reduced-motion: reduce) { canvas, .browser-page::after, .agent-field-marker, .agent-field-marker.shown { transition: none; } }
</style>
