import { CommentConnectionError, PostTimeoutError } from '$lib/api/boundedRequest';

/** Only transient request failures are retried without another press. */
export function retryCommentStatus(status: number): boolean {
	return status === 408 || status === 429 || status === 502 || status === 503 || status === 504;
}

/** Connection loss is marked at the fetch boundary; arbitrary application errors are not retried. */
export function retryCommentConnection(error: unknown): boolean {
	return error instanceof CommentConnectionError || error instanceof PostTimeoutError ||
		(error instanceof DOMException && error.name === 'AbortError');
}
