# Development Baseline

This project now uses `mine/main` as the writable development baseline.

## Remote roles

- `origin/main`: upstream reference branch from `HKUDS/nanobot`; treat as read-only unless upstream grants direct write access.
- `mine/main`: current writable baseline branch on the fork; new work should branch from here.
- local `main`: integration branch in the local clone; keep it aligned with `mine/main` for day-to-day development.

## Default workflow

1. Fetch both remotes.

   ```bash
   git fetch origin
   git fetch mine
   ```

2. Update local `main` from the fork baseline.

   ```bash
   git checkout main
   git merge --ff-only mine/main
   ```

3. Create a fresh feature branch from local `main`.

   ```bash
   git checkout -b <topic-branch>
   ```

4. Push feature work to the fork, not to upstream.

   ```bash
   git push -u mine <topic-branch>
   ```

5. When a feature is verified, merge it back into local `main`, then push local `main` to `mine/main`.

   ```bash
   git checkout main
   git merge <topic-branch>
   git push mine main:main
   ```

6. Only open upstream PRs from the fork when you intentionally want to upstream changes.

## Conservative migration rule

The branch model is migrated conservatively:

- `mine/main` is the new baseline for all future development.
- Existing historical branches are retained for reference, not for new feature work.
- Do not delete or rewrite old remote branches until their remaining value is explicitly reviewed.

## Deprecated branch policy

The following branches are now considered legacy:

- `mine/ominibot`
- `mine/feishu`
- `mine/feishu-runtime-hardening`

Rules for legacy branches:

- Do not base new feature branches on them.
- Do not merge new work into them.
- Keep them only as historical checkpoints while the team finishes migration.
- If a commit from a legacy branch is still valuable, cherry-pick or re-implement it onto `main` instead of reviving the branch.

## Legacy branch cleanup playbook

Use this checklist before deleting or archiving any legacy remote branch.

### Step 1: Freeze the branch line

- Do not open new work on the branch.
- Do not merge fresh feature work into the branch.
- Treat the branch as read-only history.

### Step 2: Check whether it still contains unique value

Inspect branch-only commits against the current baseline:

```bash
git fetch mine
git cherry -v main mine/<legacy-branch>
git log --oneline --left-right main...mine/<legacy-branch>
```

Decision rule:

- If `git cherry` shows no `+` commits, the branch no longer carries unique patch content.
- If only merge bubbles remain, the branch can usually be retired after a quick PR/reference check.
- If a branch still has one or two useful commits, port them onto `main` first, then re-run the check.

### Step 3: Check external references

Before deleting a remote branch, verify:

- no open PR still uses it as the source branch
- no automation, deployment script, or personal workflow still names it explicitly
- no collaborator is still basing active work on it

### Step 4: Prefer staged retirement over hard cleanup

Recommended order:

1. keep the branch marked as legacy in docs
2. stop using it for new work
3. port any remaining valuable commits onto `main`
4. wait for one quiet cycle
5. delete the remote branch only after the above checks stay green

### Current branch status

Based on the current migration state:

- `mine/main`: active writable baseline; keep
- `mine/feishu-runtime-hardening`: legacy; local branch already removed, remote branch can be retired later after final reference checks
- `mine/feishu`: legacy; its last useful compatibility fix has already been ported onto `main`, so it is a later deletion candidate
- `mine/ominibot`: legacy, but keep for now because the fork still points `HEAD` at it; do not delete until the fork default-branch strategy is intentionally changed

### Later cleanup commands

When you are ready for actual remote cleanup, use explicit deletes rather than force-pushes:

```bash
git push mine --delete feishu-runtime-hardening
git push mine --delete feishu
```

Leave `mine/ominibot` for the final migration phase after the fork default-branch setting is switched away from it.

## Optional local convenience

If you want local `main` to track the fork baseline directly, you can run this manually:

```bash
git branch --set-upstream-to=mine/main main
```

This is optional. The documented workflow above works even if local `main` still tracks `origin/main`.

## Working rules going forward

- Use `main`/`mine/main` as the base for all new feature branches.
- Keep upstream sync deliberate: fetch `origin/main`, inspect differences, then merge or cherry-pick intentionally.
- Do not reopen the old `feishu` or `ominibot` branch lines for routine work.
- Document any future branch-policy changes in this file before cleaning remote history.
