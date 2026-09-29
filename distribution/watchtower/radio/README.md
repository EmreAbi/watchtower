# Radio for Watchtower

This adapter bundles unchanged AgentRadio **0.7.1** runtime source from commit
`bf3d460cd66da9d65d7bb9650bd9dbe8bfb1d906`. AgentRadio is MIT licensed; its
copyright notice is preserved at `vendor/AgentRadio/LICENSE`. `provenance.json`
records SHA-256 hashes of every bundled file. This release exists as an upstream
Git tag; no upstream binary assets are included.

Watchtower supplies an absolute `RADIO_HOME` in its own data directory and
`HERDR_BIN_PATH` pointing to its executable. The adapter rejects the original
Herdr Radio ledger. Existing provider logins can be selected normally; no auth
files, transcripts, or existing ledger records are copied into this package.
Each server endpoint gets a separate `state/radio/<socket-hash>` ledger, so
named sessions cannot collide merely because their pane IDs are the same.

The private `bin/radio.cmd` (Windows) or `bin/radio` (macOS) is added only to Watchtower child processes' PATH.
The normal AgentRadio installation/startup scripts are not bundled or executed:
they update the user's shared Radio launcher. Instead this manifest starts a
hidden supervisor and relay using the same isolated state directory as every
private CLI call. The supervisor checks only its own server socket every two
seconds and stops its own child when the server stops. It never enumerates or
kills existing Radio processes. Bundle updates take effect at the next server
start; upstream's detached self-update handover is disabled in this adapter's
relay process so it cannot escape the supervisor.
While running, `relay-runtime-<supervisor-pid>.json` in the private ledger folder
records the supervisor PID, owned child PID, socket and executable for diagnostics.
The supervisor removes its own record on exit; it uses its child process handle,
never a PID read from a file, to stop the relay.
Nothing changes the registry, global PATH, or the installed Herdr plugin.

Register the portable plugin with `watchtower plugin link <package>/radio`.
Start/restart the Watchtower server afterward to run its startup hook. Joining
agents remains an explicit action; startup launches no agents and sends no
messages. Quota/account queries are never started automatically.

The **Agent Context** pane is dependency-free and refreshes every **30 seconds**.
It reads approximate per-session context metadata, not remaining account quota.
Open it with `watchtower plugin pane open --plugin radio --entrypoint context`.

The optional upstream Radio view requires Textual. From a Watchtower shell run
`<package>/radio/bin/hook.cmd install-view` to create a private virtual environment
and install it, then `radio view`. On macOS use
`sh <package>/radio/bin/hook install-view` from that Watchtower shell. This explicit dependency setup requires PyPI
access; it is not run during startup or plugin linking.

Python **3.10+** must be installed for the CLI, relay and context pane.
Validate package contents without starting anything:
`python -B <package>/radio/watchtower_radio.py verify`.
