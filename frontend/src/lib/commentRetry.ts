import { CommentConnectionError, PostTimeoutError } from '$lib/api/boundedRequest';

/** Retry after sign-in or a transient request failure without another press. */
export function retryCommentStatus(status: number): boolean {
	return status === 401 || status === 408 || status === 429 || status === 502 || status === 503 || status === 504;
}

/** Connection loss is marked at the fetch boundary; arbitrary application errors are not retried. */
export function retryCommentConnection(error: unknown): boolean {
	return error instanceof CommentConnectionError || error instanceof PostTimeoutError ||
		(error instanceof DOMException && error.name === 'AbortError');
}
