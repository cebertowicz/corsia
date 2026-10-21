import {
	SETTINGS_PATH, SETTINGS_PATH_IN_SENTENCE as openPath, codeRefusalLine, parseAddress, refusesOnCodeScreen, remoteDiscovered, remotePair, unreachableLine,
	type PairResult, type PairTarget,
} from '$lib/remoteShell';

/** How long the boxes show as refused before they clear, so the refusal is seen and the characters never pop out. */
export const REFUSAL_HOLD_MS = 200;
/** How long the search runs on an empty list before the screen says nothing was found (it keeps looking, and Look again restarts the window). */
export const NONE_FOUND_AFTER_MS = 5000;
export const DISCOVERY_POLL_MS = 1000;

type Deps = {
	discovered?: typeof remoteDiscovered;
	pair?: typeof remotePair;
	hold?: (ms: number) => Promise<void>;
};

/**
 * Setup's Connect to CORSIA on another computer (r1 to r7): the computers announcing themselves on this network
 * (names only) or a typed address, then the four-character code that computer shows. The desktop shell does the
 * finding and the pairing; this holds what the screens draw.
 */
export function createSetupRemote(deps: Deps = {}) {
	const discovered = deps.discovered ?? remoteDiscovered;
	const pair = deps.pair ?? remotePair;
	const hold = deps.hold ?? ((ms: number) => new Promise<void>((done) => setTimeout(done, ms)));

	let stage = $state<'choose' | 'code'>('choose');
	let names = $state<string[]>([]);
	let denied = $state(false);
	let lookedFor = $state(false);
	let picked = $state<string | null>(null);
	let address = $state('');
	let chooseError = $state('');
	let code = $state('');
	let invalid = $state(false);
	let refusal = $state('');
	let refocus = $state(0);
	let busy = $state(false);
	let connectedTo = $state('');
	let waited = $state(false);
	let typing = $state(false);
	let waitTimer: ReturnType<typeof setTimeout> | undefined;

	const typed = $derived(parseAddress(address));
	const target = $derived.by((): { target: PairTarget; label: string } | null => {
		if (address.trim()) return typed ? { target: { kind: 'address', host: typed.host, ...(typed.port ? { port: typed.port } : {}) }, label: address.trim() } : null;
		return picked ? { target: { kind: 'dns_sd', instance: picked }, label: picked } : null;
	});

	async function poll() {
		try {
			const result = await discovered();
			names = result.names;
			denied = result.denied === true;
			lookedFor = true;
			// A pick that disappeared from the network is no longer a choice.
			if (picked && !result.names.includes(picked)) picked = null;
		} catch {
			lookedFor = true;
		}
	}

	/** Opens the search window: Looking until it ends, then the nothing-found screen if no computer has answered. */
	function armWindow() {
		clearTimeout(waitTimer);
		waited = false;
		waitTimer = setTimeout(() => { waited = true; }, NONE_FOUND_AFTER_MS);
	}

	/** Starts looking; poll every second while r1 is open. Returns the stop function. */
	function start() {
		armWindow();
		void poll();
		const timer = setInterval(() => { if (stage === 'choose') void poll(); }, DISCOVERY_POLL_MS);
		return () => { clearInterval(timer); clearTimeout(waitTimer); };
	}


	return {
		get stage() { return stage; },
		get names() { return names; },
		get denied() { return denied; },
		get picked() { return picked; },
		get address() { return address; },
		set address(value: string) { address = value; picked = value.trim() ? null : picked; chooseError = ''; },
		get chooseError() { return chooseError; },
		get code() { return code; },
		set code(value: string) { code = value; },
		get invalid() { return invalid; },
		get refusal() { return refusal; },
		get refocus() { return refocus; },
		get busy() { return busy; },
		get connectedTo() { return connectedTo; },
		/** What the screens call the computer: the picked name, or what was typed. */
		get label() { return target?.label ?? ''; },
		/** The sidebar rows while Connect is open. */
		get steps(): { label: string; icon: 'computer' | 'code'; state: 'todo' | 'current' | 'done' }[] {
			return [
				{ label: 'Choose a computer', icon: 'computer', state: stage === 'choose' ? 'current' : 'done' },
				{ label: 'Enter the code', icon: 'code', state: stage === 'code' ? 'current' : 'todo' },
			];
		},
		/** The shell's title, lede and Continue for the stage. */
		get view(): { title: string; lede?: string; primary?: { label: string; disabled?: boolean }; onPrimary?: () => void } {
			if (stage === 'code') {
				// A typed address only answers when sharing is already on, so its lede and pictures skip the turn-on step and never show the address.
				if (target?.target.kind === 'address') return { title: 'Enter the code', lede: `On the main computer, ${openPath} and press New code. Then type the four characters here.` };
				return { title: 'Enter the code', lede: `On ${target?.label ?? ''}: ${SETTINGS_PATH}, then turn on Let other devices connect. If it’s already on, press New code. Then type the four characters here.` };
			}
			// Nothing found with no address typed: the way forward is to look again; a typed address brings Continue back.
			if (this.phase === 'none' && target === null) return { title: 'Connect to CORSIA on another computer', primary: { label: 'Look again' }, onPrimary: () => this.lookAgain() };
			return { title: 'Connect to CORSIA on another computer', primary: { label: 'Continue', disabled: target === null }, onPrimary: () => this.toCode() };
		},
		get canContinue() { return target !== null; },
		/** The choose screen's state: Looking, the list (Found), macOS blocking the search, or Nothing found once the window ends empty. */
		get phase(): 'looking' | 'found' | 'denied' | 'none' {
			if (names.length > 0) return 'found';
			if (denied) return 'denied';
			return lookedFor && waited ? 'none' : 'looking';
		},
		/** The address field appears only when asked for with "Not listed? Type its address", or when it already holds text or an error. */
		get addressOpen() { return typing || address.trim() !== '' || chooseError !== ''; },
		get typedTarget() { return target?.target.kind === 'address'; },
		get invalidAddress() { return address.trim() !== '' && typed === null; },
		start,
		poll,
		showAddress() { typing = true; },
		/** Restarts the search window from Looking. */
		lookAgain() { armWindow(); void poll(); },
		pick(name: string) { picked = name; address = ''; chooseError = ''; },
		toCode() {
			if (!target) return;
			code = ''; invalid = false; refusal = ''; chooseError = '';
			stage = 'code';
		},
		toChoose() { stage = 'choose'; code = ''; invalid = false; refusal = ''; busy = false; },
		reset() { stage = 'choose'; typing = false; picked = null; address = ''; chooseError = ''; code = ''; invalid = false; refusal = ''; busy = false; connectedTo = ''; },
		async submit(entered: string): Promise<PairResult | null> {
			if (busy || !target) return null;
			busy = true; refusal = ''; invalid = false;
			const label = target.label;
			const result = await pair(target.target, entered);
			if (result.ok) {
				// The shell moves the window to the other computer; this screen stays quiet until it does.
				connectedTo = result.name;
				return result;
			}
			if (refusesOnCodeScreen(result.reason)) {
				refusal = codeRefusalLine(result.reason, label) ?? '';
				invalid = true;
				await hold(REFUSAL_HOLD_MS);
				code = ''; invalid = false; refocus += 1;
			} else {
				refusal = unreachableLine(result.reason, label, { app: result.app_version, host: result.host_version }) ?? '';
				code = entered; stage = 'code';
			}
			busy = false;
			return result;
		},
	};
}

export type SetupRemote = ReturnType<typeof createSetupRemote>;
