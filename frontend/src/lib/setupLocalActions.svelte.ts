import { cancelLocalDownload, setupLocalSource } from './conductorSources';

const START_FAILED = 'The local model couldn’t start. Try again, or choose another source.';

/** Cancel and Try again on the local model's download row. `busy` is shared with the other local actions on the screen. */
export function createSetupLocalActions(deps: {
	token: () => string | null;
	downloading: () => boolean;
	existingOmlx: () => boolean;
	refresh: () => Promise<void>;
	/** The person has to decide about their own oMLX before anything starts. */
	askConsent: () => void;
	/** Shows the message under the rows; '' clears it. */
	fail: (message: string) => void;
	clearStartError: () => void;
}) {
	let busy = $state(false);

	async function cancel() {
		const current = deps.token();
		if (!current || busy) return;
		busy = true;
		deps.fail('');
		try {
			await cancelLocalDownload(current);
			await deps.refresh();
		} catch {
			deps.fail('CORSIA couldn’t cancel the local model download. Try again.');
		} finally {
			busy = false;
		}
	}

	async function retry() {
		const current = deps.token();
		if (!current || busy || deps.downloading()) return;
		if (deps.existingOmlx()) {
			deps.askConsent();
			return;
		}
		busy = true;
		deps.fail('');
		deps.clearStartError();
		try {
			const result = await setupLocalSource(current);
			if (result.code === 'consent_required') {
				deps.askConsent();
				return;
			}
			if (!result.ok) deps.fail(START_FAILED);
		} catch {
			deps.fail(START_FAILED);
		} finally {
			await deps.refresh();
			busy = false;
		}
	}

	return { get busy() { return busy; }, set busy(value: boolean) { busy = value; }, cancel, retry };
}
