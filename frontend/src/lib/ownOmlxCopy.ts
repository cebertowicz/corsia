/**
 * The words for the person's own oMLX (J9): Welcome's two choices, the consent they give,
 * and Settings' control. Welcome and Settings show the same consent, so it lives here once.
 */
export const OWN_OMLX_FOUND = 'CORSIA found oMLX on this computer. Both choices use it.';
export const MANAGE_LABEL = 'Let CORSIA manage it';
export const MANAGE_META = 'Recommended. CORSIA keeps your oMLX’s settings right for the model it sets up.';
export const USE_MINE_LABEL = 'Use mine';
export const USE_MINE_META = 'CORSIA uses your models and changes nothing. Change settings in oMLX’s own admin page.';
/** The disclosure's lead, set in bold before `MANAGE_DISCLOSURE`. */
export const MANAGE_DISCLOSURE_LEAD = 'Letting CORSIA manage it';
export const MANAGE_DISCLOSURE = 'means CORSIA reads your oMLX’s admin key, never changing it, and changes these settings for every app that uses this oMLX: Max Concurrent Requests and Prefill Priority, and for the model it sets up, Ctx Window, Max Tokens, TurboQuant KV Cache (bits and skip last layer), Native MTP and Thinking. Each is shown with its current and new value first. It never touches your sign-in, network settings, port, folders, default model or other models. Stopping management in Settings puts your values back.';
/** Setup's read-only consent (m5), and what managing adds to it. */
export const CONSENT_READS = ['your oMLX port setting', 'the installed oMLX version', 'your oMLX server to find its available models'];
export const CONSENT_READS_KEY = 'your oMLX’s admin key';
export const CONSENT_NO_CHANGE = 'It won’t change your oMLX.';
export const CONSENT_CHANGES = 'It changes your oMLX’s settings only after you approve the changes in chat.';
/** The This computer row when the person's oMLX is too old to say which model fits. */
export function cannotCheck(version: string): string {
	return `CORSIA couldn’t check which local AI model fits this computer: your oMLX is older than ${version}. Updating oMLX lets CORSIA check.`;
}
export const STOP_MANAGING = 'Stop managing';
export const STOP_TITLE = 'Stop managing your oMLX?';
export const RESTORE_FAILED = 'CORSIA couldn’t put your oMLX’s values back.';
export const RESTORED = 'Your oMLX’s values are back, and CORSIA no longer changes its settings.';
export const MANAGING = 'CORSIA is setting up your oMLX. It asks in chat before it changes any setting.';

/**
 * What Stop managing tells the person it will put back, and whether it forgets the key it stored.
 * oMLX asks every app for that key (unless it skips the check on this computer), so they enter it again.
 */
export function stopDescription(restores: string[], forgetsKey: boolean): string {
	const back = restores.length ? `CORSIA puts back ${restores.join('; ')}.` : 'CORSIA hasn’t changed any setting, so nothing needs putting back.';
	return forgetsKey ? `${back} Then CORSIA forgets the admin key it stored. To keep using your oMLX in CORSIA, enter that key again in CORSIA.` : back;
}
