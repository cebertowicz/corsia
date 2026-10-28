import { SERVER_UNREACHABLE } from '$lib/plainError';

/** Deactivate an agent, returning user-facing failure copy or null on success. */
export async function deactivateSidebarAgent(id: string, authToken: string): Promise<string | null> {
	let response: Response;
	try {
		const query = new URLSearchParams({ token: authToken });
		response = await fetch(`/api/agents/${id}?${query}`, { method: 'DELETE' });
	} catch (error) {
		console.error('Could not deactivate agent', error);
		return SERVER_UNREACHABLE;
	}
	if (response.ok) return null;

	const body = await response.json().catch(() => ({})) as { detail?: string; reference?: unknown } | null;
	console.error(`Deactivate failed (${response.status}): ${body?.detail ?? ''}`);
	let message: string;
	switch (response.status) {
		case 401:
			message = 'Your sign-in isn’t valid. In Settings → System, log out and sign in again.';
			break;
		case 403:
			message = 'You don’t have access to deactivate this agent. You can continue in another chat.';
			break;
		case 404:
			message = 'This agent’s chat is no longer available. Refresh the page to update the sidebar.';
			break;
		default:
			message = response.status >= 400 && response.status < 500
				? 'Couldn’t deactivate this agent. You can continue in another chat.'
				: 'Couldn’t deactivate this agent. Try again.';
	}
	return `${message}${typeof body?.reference === 'string' && body.reference ? ` Reference: ${body.reference}` : ''}`;
}
