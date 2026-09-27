// Watchtower modifications: independent preview identity, isolation or role presentation.
mod args;
mod attach;
mod host;
mod process;
mod restart_policy;
mod saved;

pub(crate) use args::*;
pub(crate) use attach::*;
pub(crate) use host::run_remote_client_bridge;
pub(crate) use saved::*;

fn ensure_remote_enabled() -> std::io::Result<()> {
    if cfg!(feature = "watchtower") {
        return Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "remote connections are disabled in this local Watchtower preview; remote Herdr installs and runtimes are unchanged",
        ));
    }
    Ok(())
}

pub(crate) fn run_remote_api_bridge(args: &[String]) -> std::io::Result<()> {
    ensure_remote_enabled()?;
    match args {
        [] => {
            let path = crate::api::socket_path();
            let stream = crate::ipc::connect_local_stream(&path).map_err(|error| {
                std::io::Error::new(
                    error.kind(),
                    format!(
                        "failed to connect to remote Herdr API socket {}: {error}",
                        path.display()
                    ),
                )
            })?;
            crate::platform::forward_remote_bridge_stdio(stream, false)
        }
        [flag] if flag == "--check" => {
            println!("herdr-api-bridge-v1");
            Ok(())
        }
        _ => Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "usage: herdr remote-api-bridge [--check]",
        )),
    }
}

pub(crate) fn print_saved_ssh_error_hint(err: &std::io::Error, target: &str) {
    if is_remote_host_key_error(err) {
        eprintln!(
            "hint: saved machines use strict host-key checking; add the host key to the configured known_hosts file, then retry."
        );
    } else {
        print_remote_error_hint(err, target);
    }
}

pub(crate) fn print_remote_error_hint(err: &std::io::Error, target: &str) {
    if is_remote_auth_error(err) {
        eprintln!(
            "hint: verify SSH access first with `{}`.",
            ssh_check_command(target)
        );
        eprintln!(
            "hint: if your SSH key has a passphrase, load it into ssh-agent with `ssh-add` before running `herdr --remote`."
        );
    }
}

fn is_remote_host_key_error(err: &std::io::Error) -> bool {
    let message = err.to_string().to_ascii_lowercase();
    message.contains("host key verification failed")
        || message.contains("remote host identification has changed")
}

fn is_remote_auth_error(err: &std::io::Error) -> bool {
    let message = err.to_string();
    message.contains("Permission denied")
        && (message.contains("(publickey")
            || message.contains("(keyboard-interactive")
            || message.contains("(password"))
}

fn ssh_check_command(target: &str) -> String {
    format!("ssh {}", shell_quote(target))
}

fn shell_quote(value: &str) -> String {
    if !value.is_empty()
        && value.chars().all(|ch| {
            ch.is_ascii_alphanumeric()
                || matches!(
                    ch,
                    '@' | '%' | '_' | '+' | '=' | ':' | ',' | '.' | '/' | '-'
                )
        })
    {
        return value.to_string();
    }

    format!("'{}'", value.replace('\'', "'\\''"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(feature = "watchtower")]
    #[test]
    fn watchtower_remote_entrypoints_reject_before_opening_connections() {
        // The invalid target is deliberate: the preview boundary must reject
        // before target discovery, SSH, runtime startup, or installer prompts.
        let launch = RemoteLaunch {
            target: "not a remote target".to_string(),
            keybindings: RemoteKeybindings::Local,
            live_handoff: true,
        };
        let failures = [
            run_remote(launch).err(),
            prepare_saved_ssh("not a remote target", "default").err(),
            connect_saved_ssh("invalid", "not a remote target", "default").err(),
            SavedSshApiBridge::start("invalid", "not a remote target", "default").err(),
            run_remote_api_bridge(&[]).err(),
            run_remote_client_bridge(&[]).err(),
        ];
        for failure in failures {
            let error = failure.expect("preview must reject remote entrypoint");
            assert_eq!(error.kind(), std::io::ErrorKind::Unsupported);
            assert!(error.to_string().contains("Watchtower preview"));
        }
    }

    #[test]
    fn remote_host_key_error_matches_ssh_diagnostics() {
        for message in [
            "Host key verification failed.",
            "REMOTE HOST IDENTIFICATION HAS CHANGED!",
        ] {
            assert!(is_remote_host_key_error(&std::io::Error::other(message)));
        }
        assert!(!is_remote_host_key_error(&std::io::Error::other(
            "server closed connection"
        )));
    }

    #[test]
    fn remote_auth_error_matches_ssh_auth_denied() {
        let err = std::io::Error::other(
            "remote platform detection failed: user@host: Permission denied (publickey).",
        );

        assert!(is_remote_auth_error(&err));
    }

    #[test]
    fn remote_auth_error_matches_keyboard_interactive_denied() {
        let err = std::io::Error::other(
            "remote server status failed: user@host: Permission denied (keyboard-interactive).",
        );

        assert!(is_remote_auth_error(&err));
    }

    #[test]
    fn remote_auth_error_ignores_non_auth_errors() {
        let err = std::io::Error::other("remote platform detection failed: unsupported platform");

        assert!(!is_remote_auth_error(&err));
    }

    #[test]
    fn ssh_check_command_quotes_remote_target() {
        assert_eq!(ssh_check_command("host name"), "ssh 'host name'");
    }
}
