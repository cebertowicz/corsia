import {
	codeRefusalLine, parseAddress, refusesOnCodeScreen, remoteDiscovered, remotePair, unreachableLine,
	type PairResult, type PairTarget,
} from '$lib/remoteShell';

/** How long the boxes show as refused before they clear, so the refusal is seen and the characters never pop out. */
export const REFUSAL_HOLD_MS = 200;
/** How long Looking… runs on an empty list before the screen says what to check (it keeps looking). */
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

	/** Starts looking; poll every second while r1 is open. Returns the stop function. */
	function start() {
		waited = false;
		void poll();
		const timer = setInterval(() => { if (stage === 'choose') void poll(); }, DISCOVERY_POLL_MS);
		const wait = setTimeout(() => { waited = true; }, NONE_FOUND_AFTER_MS);
		return () => { clearInterval(timer); clearTimeout(wait); };
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
		get canContinue() { return target !== null; },
		/** Looking… alone at first; the sentence on what to check once nothing has turned up for a while. */
		get searching() { return names.length === 0 && !denied && !waited; },
		get noneFound() { return lookedFor && names.length === 0 && !denied && waited; },
		get invalidAddress() { return address.trim() !== '' && typed === null; },
		start,
		poll,
		pick(name: string) { picked = name; address = ''; chooseError = ''; },
		toCode() {
			if (!target) return;
			code = ''; invalid = false; refusal = ''; chooseError = '';
			stage = 'code';
		},
		toChoose() { stage = 'choose'; code = ''; invalid = false; refusal = ''; busy = false; },
		reset() { stage = 'choose'; picked = null; address = ''; chooseError = ''; code = ''; invalid = false; refusal = ''; busy = false; connectedTo = ''; },
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
				chooseError = unreachableLine(result.reason, label, { app: result.app_version, host: result.host_version }) ?? '';
				code = ''; stage = 'choose';
			}
			busy = false;
			return result;
		},
	};
}

export type SetupRemote = ReturnType<typeof createSetupRemote>;
