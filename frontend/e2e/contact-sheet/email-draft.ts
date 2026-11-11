import path from 'node:path';
import { expect, type Page, type APIRequestContext } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { FONT, FOUR, NEWER, SAME_SUBJECT, markEmailDraft, mockEmailDraftContext, reply, type ServerContext } from '../support/emailDraftContext';

// The email draft view (a reply to Dana Whitfield) in the states its header and conversation list change with. Each state is its own
// draft in one chat; the runner captures every state at every width in both themes. The conversation each draft reads is a route
// mock of `GET /api/email-drafts/{artifact_id}/context`, shaped as the backend returns it, so no mail account is involved.
const FROM = 'From: Alex Rivera <alex.rivera@example.com>';
const SUBJECT = 'Subject: Re: Saturday work day: tools and parking';
const BODY = [
  'Hi Dana,', '',
  'Thanks for the count. Twelve of us can come Saturday, so the two extra pairs of loppers from the shed will be plenty. I will bring the sign-in sheet and a cooler of water.', '',
  'Could you confirm the meeting spot is still the north trailhead lot, not the church lot?', '',
  'Thanks,', 'Alex', '',
  '-- ', 'Alex Rivera', 'Trail Volunteer Coordinator',
].join('\n');
const DANA = 'To: dana.whitfield@example.com';
const draft = (...head: string[]) => [FROM, ...head, SUBJECT, '', BODY, ''].join('\n');

/** `refused`: the entries the send refuses, as the server's reading of the header names them. */
type State = { head: string[]; context: ServerContext; refused?: string[] };
const STATES: Record<string, State> = {
  // Several people added to To and Cc, with addresses long enough to wrap at a phone width.
  'long-added-recipients': {
    context: reply(),
    head: [
      `${DANA}, priya.raman-castellanos@example.com, bartholomew.wickersham-okonkwo@example.org`,
      'Cc: troop214-parents-and-leaders-committee@example.org, jordan.lee@example.com, marguerite.delacroix-hoffmann@example.com',
    ],
  },
  'one-newer-email': { context: reply({ conversation: [...NEWER[1], ...FOUR] }), head: [DANA] },
  'six-newer-emails': { context: reply({ conversation: [...NEWER[6], ...FOUR] }), head: [DANA] },
  'bad-address': { context: reply(), head: [`${DANA}, priya.raman, dana@`], refused: ['priya.raman', 'dana@'] },
  'quiet-notes-only': { context: reply({ notes: [SAME_SUBJECT, FONT] }), head: [DANA] },
  'none': { context: reply(), head: [DANA] },
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
  const drafts: Record<string, { artifact: string; docPath: string; name: string }> = {};
  for (const [index, [state, { head }]] of Object.entries(STATES).entries()) {
    // The draft tools name a draft email-<12 hex>.md in the email-drafts folder; the draft record makes it an email draft, which
    // `markEmailDraft` stands in for when the state opens.
    const name = `email-${String(index).padStart(2, '0')}${thread.replace(/[^0-9a-f]/g, '').padEnd(10, '0').slice(0, 10)}.md`;
    const docPath = path.join(workspace, 'email-drafts', name);
    const saved = await request.put('/api/files/content', { params, data: { path: docPath, text: draft(...head), thread_id: thread, create: true } });
    expect(saved.ok(), await saved.text()).toBeTruthy();
    drafts[state] = { artifact: (await saved.json()).artifact.id as string, docPath, name };
  }
  const contexts = new Map(Object.entries(drafts).map(([state, { artifact }]) => [artifact, STATES[state].context]));
  async function open(target: Page, state: string = states[0]) {
    const { artifact, docPath, name } = drafts[state];
    await mockEmailDraftContext(target, (id) => contexts.get(id) ?? reply());
    await markEmailDraft(target, docPath, STATES[state].refused);
    await target.addInitScript(({ thread, artifact, docPath, name }) => {
      localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [thread]: { tabs: [{ instanceId: 'contact-sheet', artifactId: artifact, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'contact-sheet', visible: true, activeSurface: 'artifact' } }, splitWidth: null }));
    }, { thread, artifact, docPath, name });
    await loginByUrl(target, { waitForComposer: false });
    if ((target.viewportSize()?.width ?? 0) <= 768) await target.getByRole('button', { name: 'Toggle sidebar' }).click();
    await sidebarThread(target, title).click();
    await expect(target.locator('.cm-content')).toContainText('Alex Rivera');
    await expect(target.locator('.email-conversation')).toBeVisible();
  }
  await open(page);
  return open;
}
