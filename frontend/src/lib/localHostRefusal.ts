/** The server's non-macOS refusal; other errors retain their existing presentation. */
export function localHostRefusal(body: unknown): string | null {
	if (!body || typeof body !== 'object' || !('detail' in body)) return null;
	const detail = body.detail;
	if (!detail || typeof detail !== 'object' || !('code' in detail) || !('message' in detail)) return null;
	return detail.code === 'never_local_host' && typeof detail.message === 'string' ? detail.message : null;
}
