export const MISSING_IMAGE = 'Image not found';
export const UNAVAILABLE_IMAGE = 'Image not available here';
export const NOT_AN_IMAGE = 'Not an image';
export const FAILED_IMAGE = "Couldn't load this image";

/**
 * Why an image did not load, from the server's answer to the same request: the file is missing (404), refused
 * as outside this scope (400) or not an image (415). Any other status, or no answer at all, is a plain failure.
 */
export async function imageFailureReason(src: string): Promise<string> {
	try {
		const res = await fetch(src);
		void res.body?.cancel();
		if (res.status === 404) return MISSING_IMAGE;
		if (res.status === 400) return UNAVAILABLE_IMAGE;
		if (res.status === 415) return NOT_AN_IMAGE;
	} catch { /* Offline or aborted: the answer is unknown, so it stays a plain failure. */ }
	return FAILED_IMAGE;
}
