# Watchtower for Apple Silicon

This preview targets Apple Silicon (M1 or later), macOS 15 or later. It is a
terminal application, based on Herdr, with Watchtower accounts, teams, Results,
Model Lab and the pinned private AgentRadio runtime. Intel Macs are not packaged.

## Start

Install Python 3.10+ and the agent CLIs you want to use separately. Download the
`macos-aarch64.tar.gz` and its `.sha256` file into the same folder, then verify
the download in Terminal with `shasum -a 256 -c <archive>.sha256`. Extract it into
a writable folder. From that folder:

```sh
./setup-radio
./setup-accounts
./open-watchtower
```

The second command creates Watchtower's private UI environment and installs its
pinned dependencies. Authentication is handled by the provider CLIs. The package
includes no user credentials, account profiles, conversations or private teams.
Keep the complete extracted directory together. Run `./watchtower --help` for
CLI commands. No global PATH installation or administrator privileges are needed.

The executable has an ad-hoc signature for Apple Silicon execution; it is not
Developer ID signed or notarized. macOS may require you to approve opening the
download in Privacy & Security. Do not disable Gatekeeper globally.

## Initial Mac boundaries

Accounts can connect profiles and launch new agents. Moving an existing running
Codex session to another account is initially Windows-only; select the account
when creating a new agent on Mac. Central CLI updates also retain their provider
and platform limits. Results uses Finder, the macOS clipboard and a native save
dialog. Results history currently supports Codex; other agents keep terminal UI.

The runtime uses Watchtower-specific directories. Set an absolute
`WATCHTOWER_HOME` before setup and launch to keep everything under one chosen
directory (`config` and `state` children). Watchtower does not import another
machine's accounts or sessions automatically. Its self-updater is disabled.

## Build and validation

On Apple Silicon with Xcode command line tools, the pinned Rust toolchain,
Zig 0.16.0 and Python 3:

```sh
python3 -B scripts/watchtower_build_macos.py --require-clean-source
```

The native GitHub Actions workflow runs Rust and Python tests, validates the
arm64 Mach-O binary and system-only dynamic dependencies, packages allowlisted
files with hashes and executable modes, then checks two isolated runtimes with
real zsh PTYs and Radio startup/shutdown. No paid inference or real account
login is part of CI. Interactive provider login and downloaded-app approval
still need a user's desktop session. Consult the workflow result for the exact
commit; Windows validation alone is not evidence of a successful Mac build.
