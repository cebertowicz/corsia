import type { EmailAttachment, EmailDraftContext, EmailMessage, EmailNote } from './emailDraft';

// Prototype data for the email draft view: believable volunteer-coordination conversations, newest first, with
// example.com addresses only. The real conversation comes from the backend when a draft is saved.

const DANA = 'Dana Whitfield <dana.whitfield@example.com>';
const MARCUS = 'Marcus Obi <marcus.obi@example.com>';
const PRIYA = 'Priya Raman <priya.raman@example.com>';
const JORDAN = 'Jordan Lee <jordan.lee@example.com>';
const ALEX = 'Alex Rivera <alex.rivera@example.com>';

const email = (id: string, sender: string, date: string, body: string): EmailMessage => ({ id, sender, date, body });

const FOUR: EmailMessage[] = [
	email('f4-1', DANA, '2026-11-04T17:12:00Z', [
		'Hi Alex,',
		'',
		'I counted eight sign-ups for Saturday so far, plus the three from the scout troop. We will have the two chippers booked from the county, but loppers are short: the shed only has four pairs.',
		'',
		'The church lot may be closed that morning for a bake sale, so I would rather meet at the north trailhead lot. Can you confirm you are fine with that?',
		'',
		'Dana',
	].join('\n')),
	email('f4-2', MARCUS, '2026-11-03T09:40:00Z', [
		'Dana, I can lead the brush crew again. I will need the 8:30 start so we clear the drainage cut before the rain on Sunday.',
		'',
		'Does anyone know if the gate code changed? It did not work for me last month.',
		'',
		'Marcus',
	].join('\n')),
	email('f4-3', ALEX, '2026-11-02T16:05:00Z', [
		'Hi all,',
		'',
		'Work day is Saturday, Nov 14, 9:00 to 1:00. Please reply with your name and whether you are bringing your own gloves. We will cover water and lunch.',
		'',
		'Alex',
	].join('\n')),
	email('f4-4', DANA, '2026-11-02T11:20:00Z', [
		'Alex, the county confirmed the permit for the drainage cut. Can you send the volunteer note by tonight so people can plan the weekend?',
		'',
		'Dana',
	].join('\n')),
];

const TWELVE: EmailMessage[] = [
	FOUR[0],
	email('l12-2', MARCUS, '2026-11-03T09:40:00Z', 'Dana, I can lead the brush crew again. I will need the 8:30 start so we clear the drainage cut before the rain on Sunday.\n\nMarcus'),
	email('l12-3', PRIYA, '2026-11-02T20:15:00Z', 'Count me in with the first-aid kit. I can also bring the folding table for the sign-in sheet if someone can carry it from the lot.\n\nPriya'),
	email('l12-4', ALEX, '2026-11-02T16:05:00Z', 'Hi all,\n\nWork day is Saturday, Nov 14, 9:00 to 1:00. Please reply with your name and whether you are bringing your own gloves. We will cover water and lunch.\n\nAlex'),
	email('l12-5', DANA, '2026-11-02T11:20:00Z', 'Alex, the county confirmed the permit for the drainage cut. Can you send the volunteer note by tonight so people can plan the weekend?\n\nDana'),
	email('l12-6', JORDAN, '2026-10-30T14:02:00Z', 'The scout troop has eleven kids and three leaders who can come. They would like a short safety talk at the start, if that fits.\n\nJordan'),
	email('l12-7', ALEX, '2026-10-29T18:30:00Z', 'Thanks Marcus. I will put the chainsaw crew on the second weekend instead and keep this one to hand tools.\n\nAlex'),
	email('l12-8', MARCUS, '2026-10-29T10:48:00Z', 'The big blowdown past the bridge needs a saw team, not loppers. I would not schedule that for the same day as the drainage work.\n\nMarcus'),
	email('l12-9', DANA, '2026-10-27T15:10:00Z', 'Can we plan Saturday, Nov 14? The county can lend the chippers that week and the forecast is dry until Sunday.\n\nDana'),
	email('l12-10', ALEX, '2026-10-26T09:12:00Z', 'Thanks everyone. I will draft the volunteer note once we pick a date. Dana, can you check the county calendar?\n\nAlex'),
	email('l12-11', PRIYA, '2026-10-25T21:44:00Z', 'Happy to help with first aid again. Last time we ran short on water, so I would bring a second cooler.\n\nPriya'),
	email('l12-12', ALEX, '2026-10-24T16:00:00Z', 'Hi all,\n\nIt is time to plan the fall work day. Drainage cut, brush, and the bridge approach are the big items. Reply with dates that work for you.\n\nAlex'),
];

// The longest realistic content: a long display name, long paragraphs and one unbroken URL in the opened email.
const LONG_SENDER = 'Dr. Dana Whitfield-Montgomery, Regional Trails and Watershed Coordinator, County Parks Department <dana.whitfield-montgomery@parks.county.example.com>';
const LONG_URL = 'https://www.example.com/county/parks/permits/2026/trail-work-days/saturday-november-14/drainage-cut-and-bridge-approach/volunteer-waiver-and-equipment-checklist-final-version-v7.pdf?utm_source=volunteer-note&utm_medium=email&utm_campaign=fall-work-day';
const LONG_PARAGRAPH = 'Thank you for sending the count so quickly. The county confirmed the permit for the drainage cut this morning, and the chippers are booked for the whole day, but the equipment list changed after the safety review: every volunteer who runs a lopper, a saw or a chipper needs the signed waiver on file before they start, and the scout troop leaders need to sign for each of the minors on their own forms. I know this adds a step at the sign-in table, so I would like to talk through how we handle the line at 8:30 before Saturday.';
const LONGMAIL: EmailMessage[] = [
	email('lm-1', LONG_SENDER, '2026-11-04T17:12:00Z', [
		'Hi Alex,',
		'',
		LONG_PARAGRAPH,
		'',
		`The waiver and the equipment checklist are both at ${LONG_URL} and the county asks that everyone reads the checklist before Saturday, not at the table.`,
		'',
		'Second, the parking. The church lot may be closed that morning for a bake sale, and the north trailhead lot holds about twenty cars, which is fewer than the number of people who said yes. If we carpool from the church lot we avoid the problem, but then someone has to run the shuttle, and the road to the trailhead is not fit for the long county van. I would rather decide this week than on Friday night, so please tell me which way you lean and I will tell the county.',
		'',
		'Dana',
	].join('\n')),
	FOUR[1],
	FOUR[2],
];

// Notes as the safeguard words them. Only what the person must act on is loud; an added recipient, a bad address and newer
// emails are read from the draft's own To and Cc and from the conversation, so no note repeats them.
const SAME_SUBJECT: EmailNote = { kind: 'quiet', text: 'Dana Whitfield wrote about the same subject in another thread.' };
const FONT: EmailNote = { kind: 'quiet', text: 'Couldn’t read your usual font, so this email may go out in your mail’s default font.' };

// Emails after the one the draft answers (Dana's, Nov 4), newest first, in the conversation above that one.
const MARCUS_LATER = email('n-1', MARCUS, '2026-11-05T17:40:00Z', 'Dana, a quick update: I walked the drainage cut this afternoon and the gate code did change. The new one is on the sign at the lot.\n\nMarcus');
const DANA_LATER = email('n-2', DANA, '2026-11-06T18:05:00Z', 'Alex, the county can lend a third chipper if someone with a trailer can pick it up Friday evening. Can anyone help?\n\nDana');
const NEWER_MANY: EmailMessage[] = [
	email('n-7', JORDAN, '2026-11-09T15:30:00Z', 'The scout troop is bringing its own gloves, so you can take them off the list.\n\nJordan'),
	email('n-6', PRIYA, '2026-11-08T21:10:00Z', 'The first-aid kit is restocked. I will bring it and a second cooler.\n\nPriya'),
	email('n-5', MARCUS, '2026-11-08T09:25:00Z', 'The forecast moved the rain to Sunday afternoon, so Saturday morning is fine for the drainage cut.\n\nMarcus'),
	email('n-4', DANA, '2026-11-07T19:00:00Z', 'The county confirmed the chipper pickup for Friday at 5:30.\n\nDana'),
	DANA_LATER,
	MARCUS_LATER,
];
const REPLIED_TO = 'f4-1';

// Attachments are read from the draft record, never from the notes. The prototype's files sit in the workspace folder
// the render route names in localStorage (`corsia:emailDraftFiles`), so a chip opens a real file.
const ATTACHMENTS: EmailAttachment[] = [
	{ name: 'north-trailhead-map.png', path: 'north-trailhead-map.png', size: 412_000, type: 'image' },
	{ name: 'volunteer-waiver.pdf', path: 'volunteer-waiver.pdf', size: 1_840_000, type: 'document' },
];

// The waiver was moved after the draft saved; the map is still there.
const ATTACHMENTS_MISSING: EmailAttachment[] = [ATTACHMENTS[0], { ...ATTACHMENTS[1], unavailable: true }];
// Long names, one of them an unbroken token, and many files.
const ATTACHMENTS_LONG: EmailAttachment[] = [
	{ name: 'north-trailhead-lot-and-church-lot-parking-comparison-with-shuttle-route-and-county-van-limits-final-v7.pdf', path: 'north-trailhead-lot-and-church-lot-parking-comparison-with-shuttle-route-and-county-van-limits-final-v7.pdf', size: 3_120_000, type: 'document' },
	{ name: 'drainage_cut_and_bridge_approach_crew_assignments_saturday_november_14_updated_after_safety_review_unbroken.png', path: 'north-trailhead-map.png', size: 412_000, type: 'image' },
	{ name: 'waiver.pdf', path: 'volunteer-waiver.pdf', size: 1_840_000, type: 'document' },
];
// A name with no separator and no extension, beside an ordinary one.
const UNBROKEN_NAME = 'northtrailheadlotandchurchlotparkingcomparisonwithshuttlerouteandcountyvanlimitsfinalversionsevenapprovedbycrew';
const ATTACHMENTS_UNBROKEN: EmailAttachment[] = [
	{ name: UNBROKEN_NAME, path: 'volunteer-waiver.pdf', size: 3_120_000, type: 'document' },
	ATTACHMENTS[1],
];
const MANY_NAMES = ['north-trailhead-map.png', 'volunteer-waiver.pdf', 'equipment-checklist.pdf', 'crew-assignments.pdf', 'parking-plan.png', 'county-permit.pdf', 'scout-troop-forms.pdf', 'first-aid-kit-list.pdf', 'sign-in-sheet.pdf'];
const ATTACHMENTS_MANY: EmailAttachment[] = MANY_NAMES.map((name) => ({
	name, path: name.endsWith('.png') ? 'north-trailhead-map.png' : 'volunteer-waiver.pdf', size: 240_000 + name.length * 31_000, type: name.endsWith('.png') ? 'image' : 'document',
}));

const FIXTURES: Record<string, EmailDraftContext> = {
	reply4: { notes: [], conversation: FOUR, status: 'ready' },
	// One newer email and three quiet notes.
	warn4: { notes: [SAME_SUBJECT, FONT], repliedTo: REPLIED_TO, conversation: [MARCUS_LATER, ...FOUR], status: 'ready' },
	// Two newer emails and the same quiet notes.
	loud3: { notes: [SAME_SUBJECT, FONT], repliedTo: REPLIED_TO, conversation: [DANA_LATER, MARCUS_LATER, ...FOUR], status: 'ready' },
	// Newer emails alone.
	newer1: { notes: [], repliedTo: REPLIED_TO, conversation: [MARCUS_LATER, ...FOUR], status: 'ready' },
	newer2: { notes: [], repliedTo: REPLIED_TO, conversation: [DANA_LATER, MARCUS_LATER, ...FOUR], status: 'ready' },
	// Six emails came after the one the draft answers.
	newer6: { notes: [], repliedTo: REPLIED_TO, conversation: [...NEWER_MANY, ...FOUR], status: 'ready' },
	quiet4: { notes: [SAME_SUBJECT, FONT], conversation: FOUR, status: 'ready' },
	// An entry in To that is not a full address: the safeguard names it as typed.
	badto: { notes: [], badRecipients: ['priya.raman'], conversation: FOUR, status: 'ready' },
	// Two entries that are not full addresses.
	badto2: { notes: [], badRecipients: ['priya.raman', 'dana@'], conversation: FOUR, status: 'ready' },
	// A new email: no conversation.
	new0: { notes: [], conversation: [], status: 'ready' },
	attach: { notes: [FONT], conversation: FOUR, status: 'ready', attachments: ATTACHMENTS },
	attachMissing: { notes: [], conversation: FOUR, status: 'ready', attachments: ATTACHMENTS_MISSING },
	attachLong: { notes: [], conversation: FOUR, status: 'ready', attachments: ATTACHMENTS_LONG },
	attachUnbroken: { notes: [], conversation: FOUR, status: 'ready', attachments: ATTACHMENTS_UNBROKEN },
	attachMany: { notes: [], conversation: FOUR, status: 'ready', attachments: ATTACHMENTS_MANY },
	// The send ends in a failure after the 5 seconds; a read-only tab.
	sendFails: { notes: [], conversation: FOUR, status: 'ready', sendEnds: 'failed' },
	readOnly: { notes: [], conversation: FOUR, status: 'ready', attachments: ATTACHMENTS, readOnly: true },
	// The document's live connection is down.
	offline: { notes: [], conversation: FOUR, status: 'ready', attachments: ATTACHMENTS, offline: true },
	// A comment on a line of an earlier email: the anchor names the email and the quoted text.
	comment4: {
		notes: [], conversation: FOUR, status: 'ready',
		comments: [{ messageId: 'f4-1', quote: 'loppers are short: the shed only has four pairs', text: 'Is four pairs enough for twelve people?' }],
	},
	// An agent asked for a review of this draft: the Needs-you list holds one item for it.
	review4: { notes: [], conversation: FOUR, status: 'ready', review: true },
	long12: { notes: [], conversation: TWELVE, status: 'ready' },
	one1: { notes: [], conversation: FOUR.slice(0, 1), status: 'ready' },
	longmail: { notes: [], conversation: LONGMAIL, status: 'ready' },
	loading: { notes: [], conversation: [], status: 'loading' },
	// A failed read, then the whole conversation once the person tries again.
	error: { notes: [], conversation: [], status: 'error', afterRetry: { notes: [], conversation: FOUR, status: 'ready' } },
	// The replied-to email and one older one were read; three others were not.
	partial: { notes: [], conversation: FOUR.slice(0, 2), status: 'partial', unreadable: 3 },
};

export function emailDraftFixture(name: string): EmailDraftContext | null {
	return FIXTURES[name] ?? null;
}
