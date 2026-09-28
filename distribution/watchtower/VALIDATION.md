# Watchtower 0.1.0-preview.2 validation

Windows x64 preview, based on Herdr 0.9.1 and pinned AgentRadio 0.7.1.
Validated on 2026-09-28. The portable archive records its exact source revision,
clean/dirty state, upstream revisions and each packaged file hash in
`BUILD-MANIFEST.json`. Publish only an archive built with `-RequireCleanSource`.

## Automated checks

- Accounts: 256 tests passed; Results: 168; Teams: 95; Model Lab: 164.
- Watchtower script discovery: 117 tests passed, including packaging, private
  imports, provider launch adapters and release build flags. The standalone
  private importer suite passed all 11 tests. ConPTY and UI architecture checks
  also passed in the focused packaging run.
- Rust binary suite: **3,245 passed, one failed, 10 ignored**. The single known
  platform-specific failure is described below; this is not an all-green run.
- Formatting and all-targets clippy with warnings denied passed for both the
  Watchtower feature and upstream default build.
- Native release compilation succeeded with the Watchtower product feature.
  Release flags preserve static CRT, strip debug data and remap repository,
  user and toolchain roots. The Windows PDB reference contains a filename only.

Tests use synthetic provider fixtures and do not make paid model requests.
The four existing private pack formats were also checked read-only: all loaded
under the generic loader with their original protocol/content hashes; no local
pack was rewritten. Private source names, populations and dataset fingerprints
are no longer embedded in application code or distributed examples.

## Privacy and package boundary

The candidate source tree and fork-specific published changes were audited for
credentials, personal account domains, local user paths and private project
identifiers. Gitleaks 8.30.1 produced seven source findings, all reviewed as
false positives: six upstream code/variable expressions and one public OAuth
client identifier verified against the pinned Radio source. No actual secret
was identified by these checks. This is a scoped audit, not a guarantee that a
scanner can detect every possible secret or an assertion about all historical
upstream commits.

Personal test fixtures were replaced with generic examples. Private benchmark
content and its source metadata remain local, outside the repository. The
published source retains public project ownership and upstream attribution.
Git author metadata is separate from the packaged application.

Packaging validates explicit runtime allowlists. Accounts, credentials,
conversations, personal teams, local benchmark packs, run outputs and developer
scratch files are excluded. Dependency notices and benchmark dataset attribution
are retained. Source imports require an explicit local manifest and input hashes;
private prompts are not published or silently imported into the default catalog.

The rebuilt binary was scanned for the known private account/project/path
markers found during preparation; none remained. Verify the final ZIP and its
SHA-256 sidecar together before distribution.

## Runtime checks and boundaries

The packaged executable is exercised using two disposable absolute
`WATCHTOWER_HOME` roots. The smoke verifies runtime/config/socket/Radio isolation,
role metadata, real shell output, configuration reload and cleanup of only the
owned test processes. It does not restart the user's running server or agents.

The current-workspace filter was previously checked at fixed 146x80 geometry
with one and 15 agents. Filter off/on medians were 1502.3/1602.8 microseconds for
one agent and 1608.4/1623.1 for 15. These are local supporting measurements, not
latency guarantees. Filtering precedes display-row formatting and does no
network or filesystem work in the render loop.

Results currently supports Codex structured records; other providers retain
their terminal view. Same-provider account switching initially supports idle
native Codex agents in Windows PowerShell. First-use login/trust screens can
require interaction. Model Lab subset scores are local adapted evaluations,
not official full-benchmark leaderboard scores. This archive is unsigned and
experimental; it has no automatic updater.

## Known test limitation

`platform::windows::config_backup::tests::backup_preserves_legacy` expects an
ACL descriptor beginning `D:(`, while this Windows environment returns inherited
`D:AI(...)` ACL text. The same assertion also failed on the unmodified upstream
engine in the original validation. No ACL, fixture or test was changed to hide
it. The broad Rust suite is therefore **not reported as green**.

## Reproduce

```powershell
python -B -m unittest discover -s scripts -p 'test_watchtower_*.py'
# Run each runtime suite in a separate Python process with the pinned UI dependencies.
python -B -m unittest discover -s distribution/watchtower/accounts -p 'test_*.py'
python -B -m unittest discover -s distribution/watchtower/results -p 'test_*.py'
python -B -m unittest discover -s distribution/watchtower/teams -p 'test_*.py'
python -B -m unittest discover -s distribution/watchtower/benchmarks -p 'test_*.py'
pwsh -NoProfile -File scripts/watchtower_build_windows.ps1 -RequireCleanSource
python -B scripts/watchtower_smoke_windows.py --exe <package>/watchtower.exe --report target/watchtower-smoke.json
```

The manual GitHub workflow performs the same checks with pinned Python/UI
versions and uploads artifacts only. It does not publish releases automatically.
