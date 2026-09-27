# Watchtower preview validation

Validated on Windows x64 on 2026-09-27 for `0.1.0-preview.1`, based on
Herdr `0.9.1` and bundled AgentRadio `0.7.1`. The final archive identifies its
exact source commit and packaged file hashes in `BUILD-MANIFEST.json`.

## Passed

- Native release build with the `watchtower` feature; default upstream build
  also passes `cargo check --locked --bin herdr`.
- `cargo fmt --check`, `cargo clippy --all-targets --locked --features watchtower
  -- -D warnings`, and `git diff --check`.
- 43 focused Rust tests for product isolation, sessions, preview guards and
  client reattachment after the final runtime changes.
- 51 Python packaging, Radio, ConPTY, configuration-reference and translation
  tests after the final launcher changes.
- Broader Python maintenance tests passed after rerunning the portable-pty
  group with Cargo on PATH; five platform-specific tests were skipped.
- Bun maintenance tests: 50 passed and one platform-specific skip across the
  initial run and the release-workflow group rerun with `just` on PATH.
- Microsoft ConPTY package signature, pinned content hashes and portable
  executable dependency checks during packaging.

The extracted portable archive passed the native two-instance smoke test:

1. Fresh absolute data roots and named sessions use separate configuration,
   sockets, workspaces and Radio ledgers, even when foreign routing is inherited.
2. Role/icon metadata survives the API round trip and a real ConPTY shell
   produces output.
3. Configuration reload preserves role defaults and does not edit the other
   instance's configuration.
4. Stopping one server stops its private Radio supervisor/relay while the other
   instance stays usable. Stopping the second leaves no owned processes.

This test exposed a WindowsApps Python alias escaping the caller's process Job.
The private launcher now resolves the real interpreter before execution; an
additional native Job-ownership regression test passes.

## Known test limitation

The broad Watchtower Rust binary suite ran 3,174 tests: 3,173 passed and one
failed; seven other tests were skipped. The failure is
`platform::windows::config_backup::tests::backup_preserves_legacy`: its ACL text
assertion expects `D:(`, while this machine returns an inherited `D:AI(...)`
descriptor. The identical test also fails in the previous unmodified engine
build on this computer. No machine ACL or upstream test was changed to hide it.
The full suite is therefore **not reported as green**.

## Not covered by this preview validation

Full interactive TUI visual review, live provider authentication and end-to-end
agent-to-agent Radio delivery were not exercised. No provider agents were
started. No existing Herdr server, settings, credentials or team state were
imported or modified. Full Account Stats, team templates and review automation
remain follow-up work, as described in the preview guide.

Reproduce runtime checks against a complete extracted archive:

```powershell
python -B scripts/watchtower_smoke_windows.py --exe <package>/watchtower.exe --report target/watchtower-smoke.json
```

The helper uses temporary data roots, retains process handles and cleans only
its own test processes and directories. A failed cleanup preserves its data
root for diagnosis.
