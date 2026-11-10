import fs from 'node:fs';
import path from 'node:path';
import { expect, type Page, type APIRequestContext } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from '../support/corsia';

// The email draft view (a reply to Dana Whitfield) in the states its header and conversation list change with. Each state is its own
// draft in one chat; the runner captures every state at every width in both themes. The conversation data is the prototype
// fixture named in localStorage (`src/lib/emailDraftFixtures.ts`), so no mail or backend behavior is involved.
const FROM = 'From: Alex Rivera <alex.rivera@example.com>';
const SUBJECT = 'Subject: Re: Saturday work day: tools and parking';
const BODY = [
  'Hi Dana,', '',
  'Thanks for the count. Twelve of us can come Saturday, so the two extra pairs of loppers from the shed will be plenty. I will bring the sign-in sheet and a cooler of water.', '',
  'Could you confirm the meeting spot is still the north trailhead lot, not the church lot?', '',
  'Thanks,', 'Alex', '',
  '-- ', 'Alex Rivera', 'Trail Volunteer Coordinator',
].join('\n');
const DANA = 'To: Dana Whitfield <dana.whitfield@example.com>';
const draft = (...head: string[]) => [FROM, ...head, SUBJECT, '', BODY, ''].join('\n');

type State = { head: string[]; fixture: string };
const STATES: Record<string, State> = {
  // Several people added to To and Cc, with names long enough to wrap at a phone width.
  'long-added-recipients': {
    fixture: 'reply4',
    head: [
      `${DANA}, Priya Raman-Castellanos <priya.raman-castellanos@example.com>, Bartholomew Wickersham-Okonkwo <bartholomew.wickersham-okonkwo@example.org>`,
      'Cc: Scout Troop 214 Parents and Leaders Committee <troop214-committee@example.org>, Jordan Lee <jordan.lee@example.com>, Marguerite Delacroix-Hoffmann <marguerite.delacroix-hoffmann@example.com>',
    ],
  },
  'one-newer-email': { fixture: 'newer1', head: [DANA] },
  'six-newer-emails': { fixture: 'newer6', head: [DANA] },
  'bad-address': { fixture: 'badto2', head: [`${DANA}, priya.raman, dana@`] },
  'quiet-notes-only': { fixture: 'quiet4', head: [DANA] },
  'none': { fixture: 'reply4', head: [DANA] },
};

export const states = Object.keys(STATES);
export const scrollAnchor = '.cm-content';

export async function seed(page: Page, request: APIRequestContext) {
  const title = 'Reply to Dana about Saturday';
  const thread = await createSession(request, title);
  const params = { token: TEST_TOKEN };
  const roots = (await (await request.get('/api/files/tree', { params })).json()).roots as Array<{ kind: string; path: string }>;
  const workspace = roots.find(root => root.kind === 'workspace')?.path;
  if (!workspace) throw new Error('Disposable workspace was not returned by the mock backend');
  const files = path.join(workspace, 'email-drafts', `files-contact-sheet-${thread}`);
  fs.mkdirSync(files, { recursive: true });
  const drafts: Record<string, { artifact: string; docPath: string; name: string }> = {};
  for (const [index, [state, { head }]] of Object.entries(STATES).entries()) {
    // The draft tools name a draft email-<12 hex>.md; the view recognizes the folder and that name.
    const name = `email-${String(index).padStart(2, '0')}${thread.replace(/[^0-9a-f]/g, '').padEnd(10, '0').slice(0, 10)}.md`;
    const docPath = path.join(workspace, 'email-drafts', name);
    const saved = await request.put('/api/files/content', { params, data: { path: docPath, text: draft(...head), thread_id: thread, create: true } });
    expect(saved.ok(), await saved.text()).toBeTruthy();
    drafts[state] = { artifact: (await saved.json()).artifact.id as string, docPath, name };
  }
  async function open(target: Page, state: string = states[0]) {
    const { artifact, docPath, name } = drafts[state];
    await target.addInitScript(({ thread, artifact, docPath, name, fixture, files }) => {
      localStorage.setItem('corsia:emailDraftFixture', fixture);
      localStorage.setItem('corsia:emailDraftFiles', files);
      localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [thread]: { tabs: [{ instanceId: 'contact-sheet', artifactId: artifact, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'contact-sheet', visible: true, activeSurface: 'artifact' } }, splitWidth: null }));
    }, { thread, artifact, docPath, name, fixture: STATES[state].fixture, files });
    await loginByUrl(target, { waitForComposer: false });
    if ((target.viewportSize()?.width ?? 0) <= 768) await target.getByRole('button', { name: 'Toggle sidebar' }).click();
    await sidebarThread(target, title).click();
    await expect(target.locator('.cm-content')).toContainText('Alex Rivera');
    await expect(target.locator('.email-conversation')).toBeVisible();
  }
  await open(page);
  return open;
}
