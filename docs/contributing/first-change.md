# Your first change

A practice run of the loop every change goes through: edit, run the local gate, read a failure, fix it, pass. The example is a one-line docs edit, so it needs no running app. At the end, the steps that turn a real fix into a pull request.

Do [Developer setup](dev-setup.md) first, as far as **Clone and install**. Run every command below from the checkout root.

## Make a practice branch

```bash
git switch -c practice-first-change
```

## Break a citation

Docs pages cite code by its repository path. A check fails when a cited path no longer exists, so a page can't keep pointing at a file that was moved or deleted.

Open `docs/contributing/writing-docs.md` and find `backend/tools/shell_guard.py` in the **Format** section. Misspell the file name as `shell_gaurd.py`, a file that doesn't exist, and save.

## Run the local gate

```bash
bash scripts/check-pr
```

`check-pr` runs the repository-wide checks in numbered steps; [Testing and review](testing.md#before-you-push) lists them. It does not run the backend or frontend tests. The first run sets up a pinned mypy in `.check-pr-mypy-venv`, which needs network access. If actionlint is not installed, step 0 says so and skips.

Step 5, **docs drift**, fails:

```text
MISSING PATH: docs/contributing/writing-docs.md: backend/tools/shell_gaurd.py
...
FAIL: documentation drift check failed — see output above
```

Each `MISSING PATH` line names the page, then the path it cites. `check-pr` stops at the first failing step, so later steps did not run.

## Fix it and run it again

Change the file name back to `shell_guard.py`, then run `bash scripts/check-pr` again. Every step passes, and the run ends with:

- **7/7 backend pytest — CI-OWNED**. CI runs the full backend suite on your pull request. Locally you run only the tests your change can affect ([Testing and review](testing.md#before-you-push)).
- **Local gates passed WITHOUT the owner-identity scan**, in red, unless your machine has a CORSIA workspace profile (`USER.md`). This is expected on a contributor's machine and the run still passed. The owner-identity scan looks for one person's name and contact details in public files, so it needs that person's profile; CI doesn't run it either.

## Clean up

```bash
git switch dev
git branch -D practice-first-change
```

## Make it a real change

The same loop, with the project's rules around it:

1. Pick an issue open to outside contributors and claim it. Which issues are open, and how to claim one, are in [`CONTRIBUTING.md`](../../CONTRIBUTING.md#picking-up-an-issue).
2. Branch from `dev` with the branch name `CONTRIBUTING.md` gives.
3. Make the change. For a docs page, follow [Writing docs](writing-docs.md), including the line at the end of the page that says which commit you checked it against.
4. Run the tests your change can affect, then `bash scripts/check-pr`.
5. Open the pull request against `dev`. Whether it needs a `CHANGELOG.md` line or `none` under **Release note** is in [`CONTRIBUTING.md`](../../CONTRIBUTING.md#pull-requests).
6. Accept the Contributor License Agreement on your first pull request from a fork, and read the checks and the review as [Testing and review](testing.md#after-you-push) describes.

Checked against the code at `ea522c7d6`.
