# Contributing to Watchtower

Watchtower is an independent distribution based on Herdr. Contributions here
target [EmreAbi/watchtower](https://github.com/EmreAbi/watchtower). Herdr has its
own contribution policy; opening a Watchtower issue or pull request does not
submit anything upstream.

## Bugs and improvements

Search existing issues first. For a bug, describe the observed and expected
behavior, the shortest reproduction, Watchtower version, operating system and
terminal. Include only the relevant logs with secrets and private data removed.
For larger changes, describe the use case and intended behavior before investing
in implementation so scope can be agreed upon.

## Pull requests

There is no approved-contributor allowlist. Human-authored and AI-assisted
contributions follow the same review process. Explain the problem, the change
and how it was verified. The submitter is responsible for reviewing generated
code and accurately reporting test results. Maintainers decide what to merge;
submission does not guarantee acceptance.

Create a focused branch from the repository's default branch. Preserve existing
sessions, agent identity, account isolation and protocol compatibility. Keep
platform behavior inside platform modules and include regression coverage for
meaningful behavior changes. Do not include credentials, local account state,
conversation logs, personal workspace configurations or private benchmark data.

## Development and checks

Read [AGENTS.md](AGENTS.md) for architecture and runtime boundaries. Build the
product with `--features watchtower`. Run formatting, clippy, relevant Rust tests
and the affected Python runtime/packaging suites. Test Mac-specific changes on
macOS and Windows-specific changes on Windows; report unavailable checks and
known failures explicitly. CI must not use real provider accounts or paid model
requests. Do not change fixtures merely to hide a failing test.

Windows packaging is described in [the preview guide](distribution/watchtower/README.md).
Apple Silicon packaging is described in [the Mac guide](distribution/watchtower/MACOS.md).
Use fresh absolute `WATCHTOWER_HOME` directories for smoke tests and stop only
processes created by that test. Never replace a user's live installation during
a test. Publishing a release is a maintainer action after validation.

## Licensing and attribution

Contributions use the repository's Apache-2.0 license unless a file has its own
compatible license. Preserve Herdr attribution and third-party notices, plus
the license and provenance of vendored Radio and benchmark data.
