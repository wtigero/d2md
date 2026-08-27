# Release process

This process applies to every release. Replace `<version>` with the project
version and `<tag>` with `v<version>`; for the first release these are `0.1.0`
and `v0.1.0`. Record the exact candidate commit SHA and every gate result.

## Gates

Complete these gates in order:

1. **Audit public history and freeze release content.** Before selecting a
   candidate, review the entire Git history for credentials, private paths,
   confidential content, and author identity metadata. Resolve every exposure
   decision and complete any approved history rewrite before opening the
   release PR. On a dedicated branch, finalize project/package/CLI versions,
   README installation commands, changelog entry, release notes, and security
   policy. Remove release-candidate and private-repository status text. Open a
   pull request, review the complete diff, and require green PR checks.
2. **Merge and select the candidate.** Merge only the reviewed, green release
   PR. Record the exact resulting commit on `main`; that commit is the release
   candidate. Do not edit source or documentation after this point. Any edit
   creates a new candidate and restarts the applicable gates from step 1.
3. **Green candidate CI.** Require all `main` CI checks to pass for the exact
   candidate SHA. A green run for the PR commit, an older SHA, a local-only
   test, or the dated manual checks in `docs/verification.md` is insufficient.
4. **Fresh candidate security scan.** Run and complete the repository security
   scan against the exact candidate SHA. Review and resolve or explicitly
   accept every finding. Resume a registered scan only when it targets that
   SHA; otherwise start one new registered scan and record its final result.
5. **Clean-checkout build.** From a fresh checkout of the candidate SHA,
   confirm `git status --porcelain` is empty, build the source distribution and
   wheel, and record their filenames and hashes.
6. **Twine validation.** Run `python -m twine check dist/*` on those exact
   artifacts. Stop on any metadata or rendering error.
7. **Distribution smoke tests.** Install the wheel and source distribution in
   separate fresh environments. For each, confirm installed metadata and
   `d2md --version` match `<version>`, run a representative conversion with
   `--stdout`, and confirm no output directory is created. Exercise every
   optional profile offered by the release where its platform dependencies
   are available.
8. **Tag.** Obtain an explicit human go/no-go, verify that `<tag>` does not
   already exist locally or remotely, and create it at the verified candidate
   SHA while the repository is still private. Never move or overwrite an
   existing release tag.
9. **Tagged-install smoke.** In a fresh environment, run the base Git install
   command from the README against `<tag>`, then verify `--version` and a
   representative `--stdout` conversion. Stop if the tag cannot be resolved
   or installs content that differs from the candidate.
10. **Public visibility and reporting.** Obtain a separate explicit human
    go/no-go. Confirm that the public-history decision from step 1 still
    applies, and verify public links, license, security policy, and release
    notes. Make the repository public, immediately enable GitHub private
    vulnerability reporting, and verify that **Report a vulnerability** works.
    Re-run the tagged Git install without private credentials. Do not announce
    the release; GitHub Release and PyPI publication remain separate gates.
11. **GitHub Release.** Obtain explicit approval, create the release from the
    verified tag, and attach only artifacts built and validated from the exact
    candidate SHA.
12. **PyPI publication.** Obtain separate explicit approval to upload the exact
    validated source distribution and wheel for `d2md==0.1.0`; do not rebuild
    artifacts. In one fresh environment without repository credentials, run
    `uv tool install d2md`; in another, run `pip install d2md`. Both installed
    commands must report `d2md 0.1.0` and pass a representative `--stdout`
    smoke conversion. This required gate must pass before announcement.
13. **Announcement.** Announce the release only after every preceding gate,
    including the separately approved PyPI publication, has passed and its
    evidence has been recorded.

## Non-negotiable release rules

- Never build or upload from a dirty worktree.
- Never upload stale `dist/` artifacts from another commit, branch, or release
  attempt. Move them aside before building in the clean checkout and compare
  the resulting hashes with the recorded artifacts.
- Never treat an older CI run, manual cross-platform evidence, or unfinished
  security scan as evidence for the candidate.
- Any source or documentation edit changes the candidate and invalidates prior
  exact-SHA evidence; freeze a new candidate and repeat the gates.
- Resolve history-rewrite decisions before selecting the candidate. Never
  rewrite a tagged candidate's history.
- Tag creation, public visibility, GitHub Release creation, and PyPI upload are
  separate external mutations. Each requires an explicit human go/no-go after
  its preceding gates pass; PyPI publication is required before announcement.
