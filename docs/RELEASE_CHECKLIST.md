# Release checklist

Use this before tagging a release. Every line is **pass / fail / skipped /
blocked**, with the environment named. Do not tick a box because the change was
written; tick it because the check ran.

## 1. Code and build

- [ ] `python tests/test_decoder.py` → 23/23
- [ ] `python tests/test_sparkplug_payload.py` → 12/12
- [ ] `python tests/check_settings_forwarded.py` → every setting forwarded
- [ ] `python tests/check_no_secrets.py` → no committed secret value, no tracked `.env`
- [ ] `python -m compileall -q src tests tools`
- [ ] `bash -n deploy/install.sh deploy/pipeline`
- [ ] `docker compose config` validates
- [ ] image builds from a clean checkout (`docker build .`)
- [ ] deploy scripts are mode `100755` and LF (`git ls-files -s`, `git archive | od -c`)

## 2. Functional (real stack)

- [ ] `pipeline status` → containers up, queue counts real, delivery `ok`
- [ ] batches show `confirmed == published`, `unmatched=0` at steady state
- [ ] `pipeline test` → reconcile `missing 0 / mismatched 0 / duplicates 0`
- [ ] unknown tag produces the `ALARM … not in VTS_TAGS` and does not starve others
- [ ] retention prune runs and deletes only delivered/audit rows

## 3. Failure and recovery

- [ ] VTScada down: `confirmed` freezes, backlog grows, nothing marked delivered
- [ ] VTScada up: re-BIRTH with the **same** bdSeq, delivery resumes
- [ ] reconcile after recovery → 0 missing / 0 mismatched
- [ ] wrong REST password → `credentials-rejected`, not `unreachable`
- [ ] unwritable `STATE_DIR` → ALARM + exit 2, no publishing
- [ ] process restart preserves pending rows
- [ ] host reboot → all containers auto-start, delivery resumes
- [ ] broker cold start → retries, no crash loop

## 4. Data safety

- [ ] `pipeline backup` produces a readable dump + manifest
- [ ] restore into a throwaway database reproduces the counts
- [ ] full `pipeline restore` over the live schema verified (needs a maintenance window)
- [ ] `bdseq` present in the backup and restored

## 5. Install

- [ ] `install.sh --check` passes on a supported OS and refuses an unsupported one
- [ ] fresh install on a **clean machine** follows `docs/INSTALL.md` with no questions
- [ ] re-running `install.sh` is safe and does not overwrite config/data
- [ ] secrets are not printed; `.env` is `0600`

## 6. Security

- [ ] no secret in tracked files (`git grep` for keys/tokens/passwords)
- [ ] `.env` not tracked
- [ ] 5432 not published
- [ ] dashboard password not the default
- [ ] broker auth path works when enabled

## 7. Documentation

- [ ] every command in `docs/` exists and runs (spot-check each doc)
- [ ] `CONFIGURATION.md` matches `settings.py` (52 settings)
- [ ] `README.md` status section says verified vs reported accurately
- [ ] `CHANGELOG.md` has the version and any migration note
- [ ] `docs/TESTING.md` matches what was actually run

## 8. Release

- [ ] version bumped in `pyproject.toml` and `src/pipeline/__init__.py`
- [ ] `CHANGELOG.md` dated
- [ ] tag created (`git tag -a vX.Y.Z -m …`)
- [ ] release artifact archived (tarball + `docker save` images if offline)
- [ ] rollback path stated (`docs/UPGRADING.md`)

---

## Status at 1.1.0 (2026-10-19)

| Area | Status | Environment |
|---|---|---|
| unit tests, forwarding guard, compile, bash -n | **PASS** | `your-pi`, isolated image |
| live delivery, `pipeline status`, storage reporting | **PASS** | `your-pi` + real VTScada |
| outage → backlog → recovery → reconcile | **PASS** | `your-pi` + real `VTS.exe` |
| backup + restore into throwaway DB | **PASS** | `your-pi` |
| host reboot auto-start | **PASS** | `your-pi` |
| wrong password / dead port classification | **PASS** | `your-pi` + real VTScada |
| unwritable STATE_DIR fail-fast | **PASS** | `your-pi`, isolated container |
| full `pipeline restore` over the live schema | **BLOCKED** — needs a maintenance window | — |
| clean install on a fresh machine | **BLOCKED** — no second machine; live ports in use | — |
| two simultaneously backlogged tags (fairness) | **UNVERIFIED** — only one tag has a backlog | — |

A release with a BLOCKED item is allowed only if that item is written into the
release notes as a known gap. Do not silently omit it.
