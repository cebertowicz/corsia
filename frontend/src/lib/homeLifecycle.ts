import type { Thread } from '$lib/stores/threads';
import type { View } from '$lib/homePaneConfig';

interface ChatViewportRef {
	captureBottomOffset: () => void;
	restoreBottomOffset: () => void;
}

interface HomeLifecycleOptions {
	token: () => string | null;
	activeThreadId: () => string | null;
	activeView: () => View;
	threads: () => Thread[];
	loadThreads: () => void | Promise<void>;
	loadModels: () => void | Promise<void>;
	loadSettings: () => void | Promise<void>;
	loadUpdateStatus: () => void | Promise<void>;
	pollSystem: () => void | Promise<void>;
	checkSecurityAlert: () => void | Promise<void>;
	selectThread: (id: string) => Promise<void>;
	setActiveView: (view: View) => void;
	chatPanel: () => ChatViewportRef | undefined;
	on: (event: string, cb: (data: any) => void) => () => void;
	connect: () => void;
}

function isEditableTarget(target: EventTarget | null): boolean {
	const element = target as HTMLElement | null;
	if (!element) return false;
	if (element.tagName === 'TEXTAREA') return true;
	if (element.tagName === 'INPUT') {
		const type = (element as HTMLInputElement).type;
		return type !== 'file' && type !== 'submit' && type !== 'button';
	}
	return element.isContentEditable === true;
}

function registerConnectionRefresh(options: HomeLifecycleOptions): () => void {
	let interval: ReturnType<typeof setInterval> | null = null;
	const unsubscribe = options.on('connected', () => {
		if (interval !== null) clearInterval(interval);
		options.loadThreads();
		options.loadModels();
		options.loadSettings();
		options.loadUpdateStatus();
		options.pollSystem();
		options.checkSecurityAlert();
		interval = setInterval(() => {
			options.pollSystem();
			options.loadSettings();
			// Cheap cached read: the backend only contacts the remote once per its
			// own TTL, so this tick never drives fetch frequency.
			options.loadUpdateStatus();
		}, 30000);
	});
	return () => {
		unsubscribe();
		if (interval !== null) clearInterval(interval);
	};
}

export function registerHomeLifecycle(options: HomeLifecycleOptions) {
	const teardownConnectionRefresh = registerConnectionRefresh(options);

	const unsubActiveTurns = options.on('active_turns_restored', async (data: { thread_ids: string[] }) => {
		if (options.activeView() !== 'chat' || options.activeThreadId()) return;
		const activeSids = new Set(data.thread_ids);
		let allThreads: Thread[] = [];
		for (let i = 0; i < 20; i++) {
			allThreads = options.threads();
			if (allThreads.some((s) => activeSids.has(s.id))) break;
			await new Promise((resolve) => setTimeout(resolve, 100));
		}
		const candidate = allThreads
			.filter((s) => activeSids.has(s.id))
			.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())[0];
		if (candidate) await options.selectThread(candidate.id);
	});

	function handleNavigateEvent(e: Event) {
		const detail = (e as CustomEvent).detail;
		if (detail?.view) options.setActiveView(detail.view);
	}

	const updateCanvasShift = () => {
		const vv = window.visualViewport;
		if (!vv) return;
		const offset = Math.max(0, Math.round(vv.offsetTop));
		document.documentElement.style.setProperty('--empty-parallax-offset', `${offset}px`);
		// Document drawers consume the same viewport observation when a reply opens the keyboard.
		document.documentElement.style.setProperty('--doc-visual-viewport-height', `${vv.height}px`);
		document.documentElement.style.setProperty('--doc-visual-viewport-offset', `${vv.offsetTop}px`);
	};

	const onFocusIn = (e: FocusEvent) => {
		if (!isEditableTarget(e.target)) return;
		document.documentElement.style.setProperty('--kb-toolbar-inset', '60px');
		document.documentElement.style.setProperty('--floater-bottom-pad', '4px');
		document.documentElement.style.setProperty('--messages-pad-top', 'calc(var(--safe-top) + 12px)');
		updateCanvasShift();
		options.chatPanel()?.captureBottomOffset();
		requestAnimationFrame(() => requestAnimationFrame(() => {
			updateCanvasShift();
			options.chatPanel()?.restoreBottomOffset();
		}));
	};

	const onFocusOut = (e: FocusEvent) => {
		if (!isEditableTarget(e.target)) return;
		document.documentElement.style.setProperty('--kb-toolbar-inset', '0px');
		document.documentElement.style.removeProperty('--floater-bottom-pad');
		document.documentElement.style.removeProperty('--messages-pad-top');
		document.documentElement.style.setProperty('--empty-parallax-offset', '0px');
		options.chatPanel()?.captureBottomOffset();
		requestAnimationFrame(() => requestAnimationFrame(() => {
			options.chatPanel()?.restoreBottomOffset();
		}));
	};

	updateCanvasShift();
	window.addEventListener('corsia:navigate', handleNavigateEvent);
	window.visualViewport?.addEventListener('scroll', updateCanvasShift);
	window.visualViewport?.addEventListener('resize', updateCanvasShift);
	document.addEventListener('focusin', onFocusIn);
	document.addEventListener('focusout', onFocusOut);
	if (options.token()) options.connect();

	return () => {
		teardownConnectionRefresh();
		unsubActiveTurns();
		window.removeEventListener('corsia:navigate', handleNavigateEvent);
		document.removeEventListener('focusin', onFocusIn);
		document.removeEventListener('focusout', onFocusOut);
		window.visualViewport?.removeEventListener('scroll', updateCanvasShift);
		window.visualViewport?.removeEventListener('resize', updateCanvasShift);
	};
}
