import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import BrowserLeaf from './BrowserLeaf.svelte';
import ArtifactLeaf from './ArtifactLeaf.svelte';
import { emit } from '$lib/ws/events';
import { get } from 'svelte/store';
import { openBrowserPane, threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { threads, type Thread } from '$lib/stores/threads';

class FakeSocket {
	static CONNECTING = 0;
	static OPEN = 1;
	static CLOSED = 3;
	static instances: FakeSocket[] = [];
	readyState = FakeSocket.CONNECTING;
	onopen: (() => void) | null = null;
	onmessage: ((event: { data: string }) => void) | null = null;
	onclose: (() => void) | null = null;
	sent: Record<string, unknown>[] = [];
	constructor(_url: string) { FakeSocket.instances.push(this); }
	send(raw: string) { this.sent.push(JSON.parse(raw)); }
	close() { this.readyState = FakeSocket.CLOSED; this.onclose?.(); }
	open() { this.readyState = FakeSocket.OPEN; this.onopen?.(); }
	receive(message: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(message) }); }
}

class TestPointerEvent extends MouseEvent {
	pointerId: number;
	pointerType: string;
	isPrimary = true;
	constructor(type: string, init: PointerEventInit = {}) {
		super(type, init);
		this.pointerId = init.pointerId ?? 1;
		this.pointerType = init.pointerType ?? 'mouse';
	}
}

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let drawing: string[] = [];

function pointer(type: string, x: number, y: number, pointerType = 'mouse') {
	return new TestPointerEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, pointerId: 1, pointerType });
}

function render() {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(BrowserLeaf, { target: host, props: { threadId: 'thread' } });
	flushSync();
	const socket = FakeSocket.instances[0];
	socket.open();
	socket.receive({ type: 'control', has_control: true });
	socket.receive({ type: 'frame', metadata: { deviceWidth: 100, deviceHeight: 50 }, data: '' });
	return { canvas: host.querySelector<HTMLCanvasElement>('canvas')!, socket };
}

beforeEach(() => {
	FakeSocket.instances = [];
	threadWorkspaces.set({});
	vi.stubGlobal('WebSocket', FakeSocket);
	vi.stubGlobal('PointerEvent', TestPointerEvent);
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }));
	vi.stubGlobal('Image', class { naturalWidth = 100; naturalHeight = 50; src = ''; decode() { return Promise.resolve(); } });
	vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 100, 100));
	drawing = [];
	vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
		setTransform() {},
		save() {},
		restore() {},
		beginPath() {},
		rect() {},
		clip() {},
		clearRect(...area: number[]) { drawing.push(`clear ${area.join(',')}`); },
		drawImage(_image: unknown, ...area: number[]) { drawing.push(`draw ${area.slice(4).join(',')}`); },
	} as unknown as CanvasRenderingContext2D);
	Object.defineProperties(HTMLCanvasElement.prototype, {
		setPointerCapture: { configurable: true, value() {} },
		releasePointerCapture: { configurable: true, value() {} },
		hasPointerCapture: { configurable: true, value: () => true },
	});
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('BrowserLeaf drawing', () => {
	it.each([[700, 1400], [1400, 500]])('sends the %ix%i pane on connect and after following a tab', (width, height) => {
		vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, width, height));
		const { socket } = render();
		const appearances = () => socket.sent.filter(message => message.type === 'appearance');
		expect(appearances()).toEqual([{ type: 'appearance', width, height, scale: 1, scheme: 'dark' }]);
		socket.receive({ type: 'browser_target_changed' });
		expect(appearances()).toEqual([
			{ type: 'appearance', width, height, scale: 1, scheme: 'dark' },
			{ type: 'appearance', width, height, scale: 1, scheme: 'dark' },
		]);
	});
	it('resends the pane appearance after following another page target', () => {
		const { socket } = render();
		const appearances = () => socket.sent.filter(message => message.type === 'appearance');
		expect(appearances()).toHaveLength(1);
		socket.receive({ type: 'browser_target_changed' });
		expect(appearances()).toHaveLength(2);
		expect(appearances()[1]).toEqual(appearances()[0]);
	});
	it('clears the old letterbox when the page size changes inside an unchanged pane', async () => {
		const { socket } = render();
		await vi.waitFor(() => expect(drawing).toContain('draw 0,25,100,50'));
		drawing = [];
		socket.receive({ type: 'frame', metadata: { deviceWidth: 100, deviceHeight: 50 }, data: '' });
		await vi.waitFor(() => expect(drawing).toEqual(['clear 0,0,100,100', 'draw 0,25,100,50']));
		drawing = [];
		socket.receive({ type: 'frame', metadata: { deviceWidth: 50, deviceHeight: 50 }, data: '' });
		await vi.waitFor(() => expect(drawing).toEqual(['clear 0,0,100,100', 'draw 0,0,100,100']));
	});
});

describe('BrowserLeaf clipboard shortcuts', () => {
 it('keeps each pane failure until its own source clears, showing the newest', () => {
  const { socket } = render();
  socket.receive({ type: 'pane_error', source: 'fit', error: 'Fit failed' });
  socket.receive({ type: 'pane_error', source: 'clipboard', error: 'Copy failed' });
  socket.receive({ type: 'navigate_result', error: null });
  flushSync();
  expect(page().textContent).toContain('Copy failed');
  socket.receive({ type: 'pane_error', source: 'clipboard', error: null });
  flushSync();
  expect(page().textContent).toContain('Fit failed');
  socket.receive({ type: 'pane_error', source: 'clipboard', error: 'Copy failed again' });
  socket.receive({ type: 'pane_error', source: 'fit', error: null });
  flushSync();
  expect(page().textContent).toContain('Copy failed again');
  socket.receive({ type: 'pane_error', source: 'clipboard', error: null });
  flushSync();
  expect(page().textContent).not.toContain('Copy failed again');
 });
 it('offers undo when a cut fails', async () => {
  vi.stubGlobal('ClipboardItem', class { constructor(public data: Record<string, Promise<Blob>>) {} });
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { write: vi.fn(async (items: { data: Record<string, Promise<Blob>> }[]) => { await items[0].data['text/plain']; }), readText: vi.fn() } });
  const { canvas, socket } = render();
  canvas.dispatchEvent(new KeyboardEvent('keydown', { key: 'x', code: 'KeyX', ctrlKey: true, bubbles: true, cancelable: true }));
  socket.receive({ type: 'clipboard_selection', id: 1, error: 'Copy failed' });
  await vi.waitFor(() => expect(page().textContent).toContain('Undo'));
  const undo = [...page().querySelectorAll('button')].find(button => button.textContent?.trim() === 'Undo');
  undo?.click();
  expect(socket.sent).toContainEqual({ type: 'clipboard_undo' });
  socket.receive({ type: 'pane_error', source: 'clipboard', error: 'Paste failed' });
  flushSync();
  expect(page().textContent).not.toContain('Undo');
 });
 it('copies the selected page text to the host clipboard', async () => {
  let written: Promise<Blob> | undefined;
  const write = vi.fn(async (items: { data: Record<string, Promise<Blob>> }[]) => { written = items[0].data['text/plain']; await written; });
  vi.stubGlobal('ClipboardItem', class { constructor(public data: Record<string, Promise<Blob>>) {} });
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { write, readText: vi.fn() } });
  const { canvas, socket } = render();
  canvas.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', code: 'KeyC', ctrlKey: true, bubbles: true, cancelable: true }));
  expect(socket.sent).toContainEqual({ type: 'clipboard_selection', id: 1, cut: false });
  socket.receive({ type: 'clipboard_selection', id: 1, text: 'page selection' });
  await vi.waitFor(() => expect(written).toBeDefined());
  expect(await (await written!).text()).toBe('page selection');
 });
 it('uses the host clipboard for paste and forwards select-all to the page', async () => {
  vi.stubGlobal('ClipboardItem', class { constructor(public data: Record<string, Promise<Blob>>) {} });
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { write: vi.fn(), readText: vi.fn().mockResolvedValue('host text') } });
  const { canvas, socket } = render();
  canvas.dispatchEvent(new KeyboardEvent('keydown', { key: 'v', code: 'KeyV', ctrlKey: true, bubbles: true, cancelable: true }));
  await vi.waitFor(() => expect(socket.sent).toContainEqual({ type: 'clipboard_paste', text: 'host text' }));
  canvas.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', code: 'KeyA', ctrlKey: true, bubbles: true, cancelable: true }));
  expect(socket.sent).toContainEqual(expect.objectContaining({ type: 'input_keyboard', key: 'a', modifiers: 2 }));
 });
});

describe('BrowserLeaf navigation metadata', () => {
	it.each([
		['its first frame', [{ type: 'frame', metadata: { deviceWidth: 100, deviceHeight: 50 }, data: '' }]],
		// A reconnected stream reports the page's metadata before its first frame.
		['page metadata', [
			{ type: 'browser_metadata', url: 'http://127.0.0.1:5173/probe.html', title: '' },
			{ type: 'frame', metadata: { deviceWidth: 100, deviceHeight: 50 }, data: '' },
		]],
	])('clears the empty-browser status on %s, with no status for a ready page', (_case, messages) => {
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(BrowserLeaf, { target: host, props: { threadId: 'thread' } });
		flushSync();
		const socket = FakeSocket.instances[0];
		socket.open();
		socket.receive({ type: 'browser_state', state: 'no_browser_yet' });
		flushSync();
		expect(page().textContent).toContain('Type an address above');
		expect(toolbar().textContent).not.toContain('Type an address above');
		for (const message of messages) socket.receive(message);
		flushSync();
		expect(host.textContent).not.toContain('Type an address above');
		expect(host.querySelector('.page-status')).toBeNull();
		expect(host.textContent).not.toContain('Ready');
	});

	it('shows an address led by the backend-named registrable domain after navigation', () => {
		const { socket } = render();
		socket.receive({ type: 'navigate_result', error: null, url: 'https://accounts.example.co.uk/a/long/page', domain: 'example.co.uk', title: 'Example page' });
		flushSync();
		expect(host.querySelector<HTMLInputElement>('[aria-label="Browser address"]')?.value).toBe('example.co.uk · accounts · …page');
	});

	it('leads a lookalike address with the site that serves it', () => {
		const { socket } = render();
		socket.receive({ type: 'browser_metadata', url: 'https://paypal.com.evil.com.br/login', domain: 'evil.com.br', title: 'Log in' });
		flushSync();
		expect(host.querySelector<HTMLInputElement>('[aria-label="Browser address"]')?.value).toBe('evil.com.br · paypal.com · …login');
	});

	it('updates the address and tab when an agent-side navigation reaches the stream', () => {
		openBrowserPane('thread');
		const { socket } = render();
		socket.receive({ type: 'browser_metadata', url: 'https://www.example.test/agent/open', domain: 'example.test', title: 'Agent opened this' });
		flushSync();
		expect(host.querySelector<HTMLInputElement>('[aria-label="Browser address"]')?.value).toBe('example.test · www · …open');
		expect(get(threadWorkspaces).thread.tabs.find((tab) => tab.kind === 'browser')?.title).toBe('Agent opened this');
		expect(FakeSocket.instances).toEqual([socket]);
		expect(socket.readyState).not.toBe(FakeSocket.CLOSED);
	});

	it('keeps its stream connected when page metadata relabels the browser tab', () => {
		openBrowserPane('thread');
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(ArtifactLeaf, { target: host, props: { threadId: 'thread', mode: 'expanded' } });
		flushSync();
		const socket = FakeSocket.instances[0];
		socket.open();
		socket.receive({ type: 'browser_metadata', url: 'https://www.example.test/agent/open', title: 'Agent opened this' });
		flushSync();
		expect(get(threadWorkspaces).thread.tabs.find((tab) => tab.kind === 'browser')?.title).toBe('Agent opened this');
		expect(FakeSocket.instances).toEqual([socket]);
		expect(socket.readyState).not.toBe(FakeSocket.CLOSED);
	});

	it('submits exactly the URL typed after an agent navigation', () => {
		const { socket } = render();
		socket.receive({ type: 'browser_metadata', url: 'https://www.example.test/agent/open', title: 'Agent opened this' });
		const input = host.querySelector<HTMLInputElement>('[aria-label="Browser address"]')!;
		input.dispatchEvent(new FocusEvent('focus'));
		input.value = 'https://next.example.test/person/page';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		host.querySelector('form')!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
		expect(socket.sent.at(-1)).toEqual({ type: 'navigate', url: 'https://next.example.test/person/page' });
	});

	it('replaces the full address when a selection made on focus is typed over before the next render', () => {
		const { socket } = render();
		socket.receive({ type: 'browser_metadata', url: 'https://www.example.test/agent/open', title: 'Agent opened this' });
		flushSync();
		const input = host.querySelector<HTMLInputElement>('[aria-label="Browser address"]')!;
		input.dispatchEvent(new FocusEvent('focus'));
		input.select();
		flushSync();
		input.setRangeText('https://next.example.test/person/page', input.selectionStart!, input.selectionEnd!, 'end');
		input.dispatchEvent(new Event('input', { bubbles: true }));
		host.querySelector('form')!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
		expect(socket.sent.at(-1)).toEqual({ type: 'navigate', url: 'https://next.example.test/person/page' });
	});
});

describe('BrowserLeaf opening lifecycle', () => {
	it('sends appearance after the first layout when the socket opens before it', () => {
		let afterLayout: FrameRequestCallback | undefined;
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
			afterLayout = callback;
			return 1;
		});
		vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect')
			.mockReturnValueOnce(new DOMRect(0, 0, 0, 0))
			.mockReturnValue(new DOMRect(0, 0, 100, 100));
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(BrowserLeaf, { target: host, props: { threadId: 'thread' } });
		flushSync();
		const socket = FakeSocket.instances[0];
		socket.open();
		expect(socket.sent.filter(message => message.type === 'appearance')).toEqual([]);
		afterLayout?.(0);
		expect(socket.sent).toContainEqual({ type: 'appearance', width: 100, height: 100, scale: 1, scheme: 'dark' });
	});

	async function expectOpen(url: string) {
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(`/api/browser/thread/open?url=${encodeURIComponent(url)}`, {
			method: 'POST', headers: { Authorization: 'Bearer ' }
		}));
	}

	function submit(url: string) {
		const input = host.querySelector<HTMLInputElement>('[aria-label="Browser address"]')!;
		input.dispatchEvent(new FocusEvent('focus'));
		input.value = url;
		input.dispatchEvent(new Event('input', { bubbles: true }));
		host.querySelector('form')!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
	}

	it('opens over HTTP when the address is submitted while the stream socket is connecting', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(BrowserLeaf, { target: host, props: { threadId: 'thread' } });
		flushSync();
		submit('https://example.test/connecting');
		await expectOpen('https://example.test/connecting');
	});

	it('opens over HTTP after the stream reports no browser yet', async () => {
		const { socket } = render();
		socket.receive({ type: 'browser_state', state: 'no_browser_yet' });
		submit('https://example.test/no-browser');
		await expectOpen('https://example.test/no-browser');
	});
});

describe('BrowserLeaf agent presence', () => {
	const ring = () => host.querySelector('.browser-page')!.classList.contains('agent-present');
	const seam = () => host.querySelector('.agent-seam')!.classList.contains('present');
	const marker = () => host.querySelector<HTMLElement>('.agent-field-marker');

	beforeEach(() => {
		threads.set([{ id: 'thread', title: 'Documents & Artifacts' } as Thread]);
	});

	afterEach(() => threads.set([]));

	it('shows the ring and seam avatar only while the agent acts, never on a password page', () => {
		const { socket } = render();
		flushSync();
		expect([ring(), seam()]).toEqual([false, false]);
		socket.receive({ type: 'browser_agent', age_ms: 0, field: null, label: null });
		flushSync();
		expect([ring(), seam()]).toEqual([true, true]);
		expect(host.querySelector('.agent-seam button')!.getAttribute('aria-label')).toBe('Documents & Artifacts');

		socket.receive({ type: 'browser_password_page', visible: true });
		flushSync();
		expect([ring(), seam()]).toEqual([false, false]);
		expect(host.querySelector('[aria-live="polite"]')!.textContent).toBe('');

		socket.receive({ type: 'browser_password_page', visible: false });
		flushSync();
		expect([ring(), seam()]).toEqual([true, true]);
		expect(host.querySelector('[aria-live="polite"]')!.textContent).toBe('Agent can see the page again');
		// The announcement is for assistive technology only; the toolbar shows no status for it.
		expect(host.querySelector('.status')).toBeNull();
	});

	it('shows no presence for an agent that acted longer ago than the presence window', () => {
		const { socket } = render();
		socket.receive({ type: 'browser_agent', age_ms: 60_000, field: null, label: null });
		flushSync();
		expect([ring(), seam()]).toEqual([false, false]);
	});

	it('marks the written field at the end of its label line, named for its agent, until the agent moves on', async () => {
		const { socket } = render();
		await vi.waitFor(() => expect(drawing.length).toBeGreaterThan(0));
		socket.receive({ type: 'browser_agent', age_ms: 0, field: { x: 10, y: 20, width: 60, height: 10 }, label: { x: 10, y: 5, width: 30, height: 10 } });
		flushSync();
		// The page is drawn at scale 1 from y 25 in the 100 x 100 canvas.
		expect(marker()!.classList.contains('shown')).toBe(true);
		expect(marker()!.classList.contains('above')).toBe(false);
		expect([marker()!.style.left, marker()!.style.top]).toEqual(['46px', '35px']);
		expect(marker()!.getAttribute('aria-label')).toBe('Documents & Artifacts is filling this field');
		expect(marker()!.getAttribute('title')).toBe('Documents & Artifacts is filling this field');
		expect(marker()!.textContent!.trim()).toBe('D');

		socket.receive({ type: 'browser_agent', age_ms: 0, field: null, label: null });
		flushSync();
		expect(marker()!.classList.contains('shown')).toBe(false);
		expect(ring()).toBe(true);
	});

	it('puts the marker just above an unlabelled field, off its border, and hides it when the person scrolls', async () => {
		const { canvas, socket } = render();
		await vi.waitFor(() => expect(drawing.length).toBeGreaterThan(0));
		socket.receive({ type: 'browser_agent', age_ms: 0, field: { x: 10, y: 20, width: 60, height: 10 }, label: null });
		flushSync();
		expect(marker()!.classList.contains('above')).toBe(true);
		expect([marker()!.style.left, marker()!.style.top]).toEqual(['70px', '41px']);
		await vi.waitFor(() => expect(drawing).toContain('draw 0,25,100,50'));
		canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, clientX: 50, clientY: 50, deltaY: 40 }));
		flushSync();
		expect(marker()!.classList.contains('shown')).toBe(false);
	});
});

const page = () => host.querySelector<HTMLElement>('.browser-page')!;
const toolbar = () => host.querySelector<HTMLElement>('form.toolbar')!;

describe('BrowserLeaf toolbar', () => {
	it('has no Go button and no standing status beside the address field', () => {
		render();
		flushSync();
		expect([...host.querySelectorAll('button')].map((button) => button.textContent?.trim() || button.getAttribute('aria-label')))
			.not.toContain('Go');
		expect(host.textContent).not.toContain('Shared with the agent');
		expect(host.textContent).not.toContain('Reconnect');
	});

	it('offers Reconnect only once the stream has ended', () => {
		const { socket } = render();
		socket.close();
		flushSync();
		expect(page().textContent).toContain('Browser stream ended');
		expect(toolbar().textContent).not.toContain('Browser stream ended');
		const reconnect = [...page().querySelectorAll('button')].filter((button) => button.textContent?.trim() === 'Reconnect');
		expect(reconnect).toHaveLength(1);
		expect(toolbar().querySelector('button[type=button]:not([aria-label])')).toBeNull();
	});

	it('shows opening, errors and an unavailable stream in the page area with Retry', () => {
		const { socket } = render();
		socket.receive({ type: 'navigate_result', error: 'blocked' });
		flushSync();
		expect(page().textContent).toContain('Browser could not open this URL: blocked');
		expect(toolbar().textContent).not.toContain('Browser could not open');
		expect(page().textContent).not.toContain('Reconnect');
		expect(page().textContent).toContain('Retry');
		socket.receive({ type: 'browser_state', state: 'error', reason: 'gone' });
		flushSync();
		expect(page().textContent).toContain('Browser stream unavailable: gone');
		expect(toolbar().textContent).not.toContain('Browser stream unavailable');
		// A failed stream leaves its last frame dimmed, so its status stays on the over-page pill.
		expect(page().querySelector('.page-status.over-page')).not.toBeNull();
	});
});

describe('BrowserLeaf keyboard relay', () => {
	it('preserves the modifier bits required by page shortcuts', () => {
		const { canvas, socket } = render();
		canvas.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, key: 'Meta', code: 'MetaLeft', metaKey: true }));
		canvas.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, key: 'a', code: 'KeyA', metaKey: true }));
		expect(socket.sent.filter(message => message.type === 'input_keyboard')).toEqual([
			{ type: 'input_keyboard', eventType: 'keyDown', key: 'Meta', code: 'MetaLeft', keyCode: 0, text: '', modifiers: 4 },
			{ type: 'input_keyboard', eventType: 'keyDown', key: 'a', code: 'KeyA', keyCode: 0, text: '', modifiers: 4 },
		]);
	});
});

describe('BrowserLeaf reconnect', () => {
	it('reconnects an ended stream when the agent opens its browser again', () => {
		const { socket } = render();
		socket.close();
		emit('browser_opened', { thread_id: 'thread' });
		expect(FakeSocket.instances).toHaveLength(2);
		emit('browser_opened', { thread_id: 'thread' });
		expect(FakeSocket.instances).toHaveLength(2);
	});
});

describe('BrowserLeaf letterbox input', () => {
	it('ignores mouse and touch presses outside the drawn page', () => {
		const { canvas, socket } = render();
		const before = socket.sent.length;
		canvas.dispatchEvent(pointer('pointerdown', 50, 10));
		canvas.dispatchEvent(pointer('pointerdown', 50, 10, 'touch'));
		expect(socket.sent.slice(before)).toEqual([]);
	});

	it('releases an inside press at the clamped page edge', async () => {
		const { canvas, socket } = render();
		await vi.waitFor(() => expect(drawing).toContain('draw 0,25,100,50'));
		canvas.dispatchEvent(pointer('pointerdown', 50, 50));
		canvas.dispatchEvent(pointer('pointerup', 50, 110));
		expect(socket.sent.filter(message => message.type === 'input_mouse')).toEqual([
			{ type: 'input_mouse', eventType: 'mouseMoved', x: 50, y: 25, button: 'none', clickCount: 0 },
			{ type: 'input_mouse', eventType: 'mousePressed', x: 50, y: 25, button: 'left', clickCount: 1 },
			{ type: 'input_mouse', eventType: 'mouseReleased', x: 50, y: 50, button: 'left', clickCount: 1 },
		]);
	});

	it('forwards wheel axes in pixels without turning vertical input horizontal', async () => {
		const { canvas, socket } = render();
		await vi.waitFor(() => expect(drawing).toContain('draw 0,25,100,50'));
		canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, clientX: 50, clientY: 50, deltaX: 3, deltaY: 90, deltaMode: 0 }));
		await vi.waitFor(() => expect(socket.sent.at(-1)).toEqual({ type: 'input_mouse', eventType: 'mouseWheel', x: 50, y: 25, deltaX: 3, deltaY: 90 }));
	});

	it('converts a line wheel to pixels, as CDP expects', async () => {
		const { canvas, socket } = render();
		await vi.waitFor(() => expect(drawing).toContain('draw 0,25,100,50'));
		canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, clientX: 50, clientY: 50, deltaX: 0, deltaY: 3, deltaMode: 1 }));
		await vi.waitFor(() => expect(socket.sent.at(-1)).toEqual({ type: 'input_mouse', eventType: 'mouseWheel', x: 50, y: 25, deltaX: 0, deltaY: 120 }));
	});
});
