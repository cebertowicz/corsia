# Looking up the person, then asking what's left

For the CORSIA agent: step 5 of `references/first-run-app.md` sends you here. Do the steps in order. Post only the lines in backticks, word for word. Never show how sure you are.

## A. What you start from

1. Name: `user_profile` `name`. When it is empty, post `What's your full name? I'll use it to look you up.` and wait for the answer.
2. Domain: the part of the person's email after the `@` (the `- **Email:**` line of the workspace's `context/50-USER-PROFILE.md`). Use it only when it is not a consumer mail domain (not gmail.com, googlemail.com, outlook.com, hotmail.com, live.com, yahoo.com, icloud.com, me.com, proton.me, aol.com). Never put the full address in a query.
3. What the import kept: their work, their city, a GitHub handle or website.
4. This Mac's time zone: the `Timezone` line of the workspace `USER.md` if it has one, else `date +%Z`.
5. Too little to go on means: you have only a name, or only a name and a consumer-mail address, and the import kept nothing. Then go to section E, post question 1 alone first, and use the answer as your detail. Then continue at section B.
6. When search does not work (`web_search_ready` is false and they haven't just saved a key) or they said not to look them up: go to section E.

## B. Search

Post `Looking you up.` once. Then:

1. Run `web_search` with the name plus one detail from A (the domain's company, else the city, else the kind of work).
2. Read each result's page with `web_fetch` before you use anything from it. A search snippet is never a fact.
3. Stop at 6 `web_search` calls and 6 `web_fetch` calls in total. Then go with what you have.
4. Never open or use a people-search or data-broker site (Spokeo, Whitepages, BeenVerified, Radaris, Intelius and the like).
5. Never show or keep a home address, phone number, birth date, family, health, money, religion or politics, even when a page has them.

## C. Decide who is the person

Do these in order.

1. **Group by person.** Put each page with one person, by what links them: the same employer, the same handle or website, the same city and kind of work, or one page linking to the other. A page that shares only the name goes in no group and is never shown.
2. **A group is the person** when its name matches plus one strong match (the person's employer, their work email's domain, a handle or website from the import, or their exact email on the page) or two weak ones (their region, their kind of work, their school), and nothing on its pages contradicts what you were given.
3. **Narrow before asking.** When more than one group is the person, search again with a detail you haven't used, then redo steps 1 and 2. When no group is the person, do the same once. Count these searches in the limit of 6.
4. **Never mix.** Every line you show comes from a page in the one group.
5. **Newest wins.** When two pages disagree, use the newer one and show its year.
6. **Links** are copied from the search result or the page you read, never written from memory.

Then pick one outcome:

- **One group is the person.** Go to outcome 1.
- **More than one group is still the person.** Go to outcome 2.
- **No group is the person, but one page is a weak fit** (one page, or the name plus one weak detail): outcome 3.
- **Nothing, search failed, or they said not to look them up:** outcome 4.

## D. The four outcomes

1. **Found.** Post `Here's what I found. Tell me if any of it is wrong.` and under it one short line per fact: the fact, its link, and the page's year when it is dated. Keep what they don't correct.
2. **More than one person.** Ask one open question that tells the groups apart, two at most, and give away nothing about the others: `Which city are you in?`, never `Portland or Denver?`. Show only what belongs to the group that matches. If they say neither, you found someone else: go to outcome 3 or 4. Only when no question can split the groups, list them one line each (their work and city) with `None of these` as the last line.
3. **Very little.** Post `You keep a low profile online. Good for your privacy, less for me to go on. Here's the little I found, and it may not be you:` then those lines, then the questions.
4. **Nothing.** Go straight to the questions. If search failed, first post `I couldn't search just now.`

**"That's not me."** Drop every line from that group. Ask the splitting question (outcome 2). Search once more. If it still doesn't fit, go to the questions.

## E. The questions

Post these in one message. Leave out any part the person already answered or the import already kept. Always ask questions 2 and 3, even when the search found plenty.

1. `What should I call you, what do you do, and where are you based?`
2. `What are you working toward right now, at work or outside it?`
3. `When I do something for you, should I check with you first or just do it and tell you after? And do you like answers short, or with the reasoning?`

Follow up once on anything vague. On "not now", carry on and learn as you go.

## F. Write it down

1. Write into the workspace `USER.md` only what they said or didn't correct, the way `references/project-import.md` does for role and working style. Where they are based sets their time zone.
2. If the name they go by is not the one you searched, run one more search with it (within the limit of 6), show what it finds the same way, and ask nothing more about it.

## Worked examples

**1. One clear match.** Inputs: name `Dana Ruiz`; email `dana@harborworks.com` (a company domain); the import kept `Operations lead`. You search `Dana Ruiz Harbor Works`. Page A is the company's team page (Dana Ruiz, Operations lead, 2025); page B is a conference talk listing (Dana Ruiz, Harbor Works). Grouping: A and B share the employer, so one group. Decision: the name matches plus the employer, which is a strong match, so this group is the person. Post: `Here's what I found. Tell me if any of it is wrong.` / `Operations lead at Harbor Works (2025): https://harborworks.com/team` / `Spoke at the Port Logistics Summit: https://portlogistics.example/talks`. Then post questions 2 and 3 in one message. Question 1 is left out because the pages answered what they do and where they work; ask only `Where are you based?` if no page said.

**2. Two people with the same name.** Inputs: name `Sam Lee`; Gmail address; nothing imported. Too little to go on, so ask question 1 first. They answer `I'm Sam, a nurse in Denver.` You search `Sam Lee nurse Denver` and find group 1 (a nurse in Denver, hospital staff page) and group 2 (a Sam Lee who is a nurse in Austin, one article). Group 1 matches the name plus kind of work plus region (two weak), so it is the person; group 2's region contradicts. Show only group 1's lines. If both had matched, you would ask `Which hospital do you work at?`, open, then show only the match.

**3. A famous namesake.** Inputs: name `Chris Evans`; Gmail address; the import kept `Software engineer, Leeds`. Results are full of the actor. Grouping: every actor page shares only the name, so it belongs to no group and is never shown. One page, a Leeds developer's GitHub profile, shares the city and kind of work: two weak matches, so that group is the person. Post `Here's what I found. Tell me if any of it is wrong.` with only that group's lines.

**4. Nothing found.** Inputs: name `Priya Nair`; Gmail address; nothing imported. Ask question 1 first; they answer `Priya, a teacher in Pune.` You run 2 searches, read 1 page, and no group matches the name plus a strong or two weak details. Search once more narrowed by `teacher Pune`; still nothing. Outcome 4: post nothing about findings, and go straight to questions 2 and 3 (question 1 is answered).
