//! Watchtower additions: opt-in product identity and process-local isolation.
//! Legacy HERDR_* names remain the plugin protocol inside this installation.

use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};

pub const VERSION: &str = "0.1.0-preview.1";
pub const DEFAULT_CONFIG: &str = include_str!("../distribution/watchtower/config.toml");
const MARKER: &str = "WATCHTOWER_CONTEXT";
const OVERRIDES: &[&str] = &[
    "CONFIG_PATH",
    "SOCKET_PATH",
    "CLIENT_SOCKET_PATH",
    "SESSION",
];

pub const fn enabled() -> bool {
    cfg!(feature = "watchtower")
}

pub fn command_name() -> &'static str {
    if enabled() {
        "watchtower"
    } else {
        "herdr"
    }
}

pub fn data_home() -> Option<PathBuf> {
    std::env::var_os("WATCHTOWER_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

fn same_install(executable: &Path, inherited: Option<&OsString>) -> bool {
    let Some(inherited) = inherited else {
        return false;
    };
    let (Some(left), Some(right)) = (executable.parent(), Path::new(inherited).parent()) else {
        return false;
    };
    match (left.canonicalize(), right.canonicalize()) {
        (Ok(left), Ok(right)) => left == right,
        _ => false,
    }
}

fn isolated_environment(
    mut env: BTreeMap<OsString, OsString>,
    from_this_install: bool,
) -> BTreeMap<OsString, OsString> {
    if !from_this_install {
        env.retain(|key, _| {
            let name = key.to_string_lossy().to_ascii_uppercase();
            !name.starts_with("HERDR_") && !name.starts_with("RADIO_")
        });
    }
    if env.contains_key(&OsString::from("WATCHTOWER_SESSION")) {
        env.remove(&OsString::from("HERDR_SOCKET_PATH"));
        env.remove(&OsString::from("HERDR_CLIENT_SOCKET_PATH"));
    }
    // Consume explicit overrides once; children inherit resolved legacy values,
    // including changes made by `--session` after startup.
    for suffix in OVERRIDES {
        if let Some(value) = env.remove(&OsString::from(format!("WATCHTOWER_{suffix}"))) {
            if value.is_empty() {
                env.remove(&OsString::from(format!("HERDR_{suffix}")));
            } else {
                env.insert(OsString::from(format!("HERDR_{suffix}")), value);
            }
        }
    }
    env.insert(MARKER.into(), "1".into());
    env
}

/// Runs before threads or argument dispatch. Never imports another product's
/// runtime routing; agent account environment variables are left unchanged.
pub fn prepare_environment() -> io::Result<()> {
    if !enabled() {
        return Ok(());
    }
    if let Some(root) = data_home() {
        if !root.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "WATCHTOWER_HOME must be an absolute path",
            ));
        }
    }
    let executable = std::env::current_exe()?;
    let original: BTreeMap<OsString, OsString> = std::env::vars_os().collect();
    let from_this_install = original
        .get(&OsString::from(MARKER))
        .is_some_and(|value| value == "1")
        && same_install(&executable, original.get(&OsString::from("HERDR_BIN_PATH")))
        && original.get(&OsString::from("WATCHTOWER_CONTEXT_HOME"))
            == Some(&crate::config::config_dir().into_os_string());
    let resolved = isolated_environment(original.clone(), from_this_install);
    for key in original.keys().filter(|key| !resolved.contains_key(*key)) {
        std::env::remove_var(key);
    }
    for (key, value) in resolved {
        std::env::set_var(key, value);
    }
    std::env::set_var("WATCHTOWER_CONTEXT_HOME", crate::config::config_dir());
    let own_binary = executable
        .parent()
        .map(|directory| directory.join("watchtower.exe"));
    let own_binary = own_binary
        .as_deref()
        .filter(|path| path.is_file())
        .unwrap_or(&executable);
    std::env::set_var("HERDR_BIN_PATH", own_binary);
    if let Some(directory) = executable.parent() {
        let mut paths = vec![directory.to_path_buf()];
        let radio_bin = directory.join("radio").join("bin");
        if radio_bin.is_dir() {
            paths.push(radio_bin);
        }
        paths.extend(
            std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
                .filter(|path| path != directory),
        );
        let path = std::env::join_paths(paths)
            .map_err(|err| io::Error::new(io::ErrorKind::InvalidInput, err))?;
        std::env::set_var("PATH", path);
    }
    Ok(())
}

fn radio_home_for_socket(base: &Path, socket: &Path) -> PathBuf {
    let digest = format!(
        "{:x}",
        Sha256::digest(socket.as_os_str().to_string_lossy().as_bytes())
    );
    base.join(&digest[..16])
}

fn context_changed(previous: Option<&std::ffi::OsStr>, endpoint: &Path) -> bool {
    previous != Some(endpoint.as_os_str())
}

/// Session parsing must run first: named servers reuse pane identifiers, so each
/// endpoint needs its own Radio ledger and relay lock, not just its own product.
pub fn prepare_session_environment() -> io::Result<()> {
    if !enabled() {
        return Ok(());
    }
    let endpoint = crate::session::active_api_socket_path();
    if context_changed(
        std::env::var_os("WATCHTOWER_CONTEXT_ENDPOINT").as_deref(),
        &endpoint,
    ) {
        for key in [
            "RADIO_HANDLE",
            "RADIO_JOINED_SCOPE",
            "RADIO_VIEW_FREQUENCY",
            "HERDR_PANE_ID",
            "HERDR_TAB_ID",
            "HERDR_WORKSPACE_ID",
        ] {
            std::env::remove_var(key);
        }
    }
    std::env::set_var("WATCHTOWER_CONTEXT_ENDPOINT", &endpoint);
    let base = std::env::var_os("WATCHTOWER_RADIO_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::config::state_dir().join("radio"));
    if !base.is_absolute() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "WATCHTOWER_RADIO_HOME must be an absolute path",
        ));
    }
    std::env::set_var("RADIO_HOME", radio_home_for_socket(&base, &endpoint));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn environment(items: &[(&str, &str)]) -> BTreeMap<OsString, OsString> {
        items
            .iter()
            .map(|(k, v)| ((*k).into(), (*v).into()))
            .collect()
    }

    #[test]
    fn foreign_routing_is_removed_without_touching_provider_accounts() {
        let result = isolated_environment(
            environment(&[
                ("HERDR_SOCKET_PATH", "official.sock"),
                ("HERDR_CONFIG_PATH", "official.toml"),
                ("HERDR_SESSION", "production"),
                ("HERDR_PANE_ID", "w1:p1"),
                ("HERDR_ENV", "1"),
                ("RADIO_HANDLE", "foreign-lead"),
                ("RADIO_JOINED_SCOPE", "freq.foreign"),
                ("RADIO_VIEW_FREQUENCY", "foreign"),
                ("CODEX_HOME", "existing-login"),
            ]),
            false,
        );
        assert!(!result
            .keys()
            .any(|key| key.to_string_lossy().starts_with("HERDR_")
                || key.to_string_lossy().starts_with("RADIO_")));
        assert_eq!(
            result.get(&OsString::from("CODEX_HOME")),
            Some(&OsString::from("existing-login"))
        );
    }

    #[test]
    fn own_pane_routing_round_trips_and_explicit_override_is_consumed() {
        let result = isolated_environment(
            environment(&[
                ("HERDR_SOCKET_PATH", "own.sock"),
                ("HERDR_PANE_ID", "w1:p1"),
                ("HERDR_SESSION", "test"),
                ("RADIO_HANDLE", "own-lead"),
            ]),
            true,
        );
        assert_eq!(
            result.get(&OsString::from("HERDR_SOCKET_PATH")),
            Some(&OsString::from("own.sock"))
        );
        assert_eq!(
            result.get(&OsString::from("HERDR_SESSION")),
            Some(&OsString::from("test"))
        );
        assert!(!result.contains_key(&OsString::from("WATCHTOWER_SESSION")));
        assert_eq!(isolated_environment(result.clone(), true), result);
    }

    #[test]
    fn explicit_watchtower_routing_replaces_foreign_routing() {
        let result = isolated_environment(
            environment(&[
                ("HERDR_SOCKET_PATH", "official.sock"),
                ("WATCHTOWER_SOCKET_PATH", "custom.sock"),
            ]),
            false,
        );
        assert_eq!(
            result.get(&OsString::from("HERDR_SOCKET_PATH")),
            Some(&OsString::from("custom.sock"))
        );
    }

    #[test]
    fn explicit_session_clears_old_socket_but_keeps_explicit_new_socket() {
        for new_socket in [None, Some("new.sock")] {
            let mut values = environment(&[
                ("HERDR_SOCKET_PATH", "old.sock"),
                ("HERDR_CLIENT_SOCKET_PATH", "old-client.sock"),
                ("WATCHTOWER_SESSION", "new-session"),
            ]);
            if let Some(socket) = new_socket {
                values.insert("WATCHTOWER_SOCKET_PATH".into(), socket.into());
            }
            let result = isolated_environment(values, true);
            assert_eq!(
                result.get(&OsString::from("HERDR_SOCKET_PATH")),
                new_socket.map(OsString::from).as_ref()
            );
            assert!(!result.contains_key(&OsString::from("HERDR_CLIENT_SOCKET_PATH")));
            assert_eq!(
                result.get(&OsString::from("HERDR_SESSION")),
                Some(&OsString::from("new-session"))
            );
        }
    }

    #[test]
    fn product_defaults_parse_and_match_manifest() {
        let config: crate::config::Config = toml::from_str(DEFAULT_CONFIG).expect("valid defaults");
        assert!(!config.update.version_check);
        assert!(!config.update.manifest_check);
        let product: serde_json::Value =
            serde_json::from_str(include_str!("../distribution/watchtower/product.json"))
                .expect("valid product");
        assert_eq!(product["version"], VERSION);
    }

    #[test]
    fn named_servers_do_not_share_radio_ledger_or_lock() {
        let base = Path::new("private-radio");
        let default = radio_home_for_socket(base, Path::new("watchtower/herdr.sock"));
        let named = radio_home_for_socket(base, Path::new("watchtower/sessions/work/herdr.sock"));
        assert_ne!(default, named);
        assert!(!context_changed(
            Some(std::ffi::OsStr::new("watchtower/herdr.sock")),
            Path::new("watchtower/herdr.sock")
        ));
        assert!(context_changed(
            Some(std::ffi::OsStr::new("watchtower/herdr.sock")),
            Path::new("watchtower/sessions/work/herdr.sock")
        ));
        assert_eq!(
            default,
            radio_home_for_socket(base, Path::new("watchtower/herdr.sock"))
        );
    }
}
