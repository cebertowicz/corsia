import type { Page, APIRequestContext } from '@playwright/test';
import { seedDocument } from './document-comments';

export const scrollAnchor = '.cm-content';

export function seed(page: Page, request: APIRequestContext) {
  return seedDocument(page, request, true, true);
}
