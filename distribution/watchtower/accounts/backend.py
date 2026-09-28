"""Portable, credential-free account profiles for the Watchtower Accounts UI.

Profiles describe a provider home, never a password, API key or access token.
Radio is the authority for launch bindings. A binding is not proof of the
identity used by an already running process. Merely opening this module or
listing accounts never creates a ledger, changes a login, or starts an agent.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

try:
    from .account_reader import CodexAccountReader, _cleanup, _email_domain, _mask_email
except ImportError:
    from account_reader import CodexAccountReader, _cleanup, _email_domain, _mask_email

VERSION = 1
PROVIDERS = ("codex", "opencode", "gemini", "claude")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z")
WORKSPACE_RE = re.compile(r"w[1-9][0-9]*\Z")
UNSET_ENV = (
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ACCESS_TOKEN", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
    "OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY", "OPENCODE_SERVER_PASSWORD", "OPENCODE_PASSWORD",
    "OPENCODE_SERVER_USERNAME", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_CONFIG_DIR", "OPENCODE_CLI_CONFIG_CONTENT", "OPENCODE_SERVER_URL", "OPENCODE_HOST",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_RESOURCE",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA", "GEMINI_DEFAULT_AUTH_TYPE",
    "GOOGLE_CLOUD_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE", "GEMINI_FORCE_FILE_STORAGE",
    "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL",
)
_GEMINI_BLANK_ENV = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA", "GEMINI_DEFAULT_AUTH_TYPE",
    "GOOGLE_CLOUD_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL",
)
_GEMINI_AUTH_SQL = ",".join("'" + key + "'" for key in _GEMINI_BLANK_ENV)
_HOME_ENV = {"codex": "CODEX_HOME", "opencode": "XDG_DATA_HOME",
             "gemini": "GEMINI_CLI_HOME", "claude": "CLAUDE_CONFIG_DIR"}
_GEMINI_HOME_MIN_VERSION = (0, 61, 0)


class AccountError(Exception):
    """A stable code and safe UI message; never include subprocess output."""

    def __init__(self, code, message=None):
        self.code = code
        self.message = message or code.replace("_", " ").capitalize()
        super().__init__(self.message)


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _name(value):
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise AccountError("invalid_name", "Use 1–48 letters, numbers, hyphens or underscores.")
    return value


def _label(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 64:
        raise AccountError("invalid_label", "Use a label of 1–64 characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AccountError("invalid_label", "The label cannot contain control characters.")
    return value.strip()


def _path(value):
    try:
        if not value or not isinstance(value, (str, os.PathLike)):
            raise ValueError()
        result = Path(value).expanduser()
        if not result.is_absolute():
            raise ValueError()
        return result.resolve()
    except (OSError, ValueError, RuntimeError):
        raise AccountError("invalid_home", "Choose an absolute account directory.") from None


def _same_path(left, right):
    return os.path.normcase(str(_path(left))) == os.path.normcase(str(_path(right)))


def _quiet_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def provider_command(provider):
    """Resolve a native executable or Node script, never execute a shell shim."""
    if provider not in PROVIDERS:
        raise AccountError("unsupported_provider")
    native = shutil.which(provider + ".exe") if os.name == "nt" else shutil.which(provider)
    if native:
        return [str(Path(native).resolve())]
    shim = shutil.which(provider + ".cmd") if os.name == "nt" else None
    if shim:
        root = Path(shim).resolve().parent / "node_modules"
        candidates = ([root / "@opencode/cli/bin/opencode.exe"] if provider == "opencode" else [])
        for executable in candidates:
            if executable.is_file():
                return [str(executable)]
        scripts = {
            "codex": [root / "@openai/codex/bin/codex.js"],
            "opencode": [root / "opencode-ai/bin/opencode"],
            "gemini": [root / "@google/gemini-cli/bundle/gemini.js",
                       root / "@google/gemini-cli/dist/index.js"],
            "claude": [root / "@anthropic-ai/claude-code/cli.js"],
        }[provider]
        node = shutil.which("node.exe") or shutil.which("node")
        for script in scripts:
            if node and script.is_file():
                return [node, str(script)]
    raise AccountError("cli_unavailable", f"Install the {provider} CLI before connecting this account.")


def plan_environment(plan, environ=None):
    """Apply only the plan's explicit home variables after removing auth overrides."""
    result = dict(os.environ if environ is None else environ)
    denied = {key.upper() for key in (*plan.get("unset_env", ()), *plan.get("env", {}))}
    for key in list(result):
        if key.upper() in denied:
            result.pop(key, None)
    result.update(plan.get("env", {}))
    return result


def default_home(environ=None):
    """An explicit Watchtower home wins; fresh portable installs need none."""
    env = os.environ if environ is None else environ
    if env.get("WATCHTOWER_HOME"):
        return _path(env["WATCHTOWER_HOME"])
    product = "watchtower-dev" if Path(env.get("WATCHTOWER_CONTEXT_HOME", "")).name == "watchtower-dev" else "watchtower"
    if env.get("XDG_STATE_HOME"):
        return _path(env["XDG_STATE_HOME"]) / product
    if os.name == "nt":
        base = env.get("LOCALAPPDATA") or Path.home() / "AppData/Local"
        return _path(base) / product
    return Path.home() / ".local/state" / product


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_json(path, fallback):
    if not path.exists():
        return deepcopy(fallback)
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError()
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or result.get("version") != VERSION:
            raise ValueError()
        return result
    except (OSError, UnicodeError, ValueError):
        raise AccountError("invalid_profile_store", "The Accounts metadata could not be read; it was left unchanged.") from None


class AccountService:
    def __init__(self, home=None, radio_home=None, radio_command=None, reader=None):
        self.home = _path(home) if home else default_home()
        self.path = self.home / "config" / "accounts.json"
        state = radio_home or os.environ.get("RADIO_HOME")
        self.radio_home = _path(state) if state else None
        radio = Path(__file__).resolve().parent.parent / "radio/vendor/AgentRadio/bin/radio"
        self.radio_command = list(radio_command) if radio_command else [sys.executable, "-B", str(radio)]
        self.reader = reader or CodexAccountReader()
        self._status = {}
        self._standalone = {}
        self._gemini_isolation = {}

    @contextmanager
    def _lock(self):
        lock = self.path.with_suffix(".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise AccountError("accounts_busy", "Another Accounts update is in progress.") from None
        try:
            os.close(descriptor)
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _store(self):
        result = _load_json(self.path, {"version": VERSION, "profiles": [], "defaults": {"default_profile": None}})
        if not isinstance(result.get("profiles"), list) or not isinstance(result.get("defaults"), dict):
            raise AccountError("invalid_profile_store")
        return result

    def _ledger(self):
        """Read only named homes and binding metadata, never account env values."""
        if self.radio_home is None or not (self.radio_home / "radio.db").is_file():
            return [], []
        try:
            uri = (self.radio_home / "radio.db").as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
                connection.row_factory = sqlite3.Row
                accounts = [dict(row) for row in connection.execute(
                    "SELECT name,provider,home,CASE WHEN env IS NULL OR env IN ('','{}') THEN 0 ELSE 1 END AS custom_env, "
                    "CASE WHEN json_valid(env) THEN json_extract(env,'$.CODEX_HOME') END AS codex_home, "
                    "CASE WHEN json_valid(env) THEN json_extract(env,'$.CLAUDE_CONFIG_DIR') END AS claude_home, "
                    "CASE WHEN json_valid(env) THEN json_extract(env,'$.GEMINI_CLI_HOME') END AS gemini_home, "
                    "CASE WHEN json_valid(env) THEN json_extract(env,'$.GEMINI_FORCE_ENCRYPTED_FILE_STORAGE') END AS gemini_storage, "
                    "CASE WHEN json_valid(env) THEN (SELECT COUNT(*) FROM json_each(accounts.env) "
                    f"WHERE key IN ({_GEMINI_AUTH_SQL})) ELSE 0 END AS gemini_auth_count, "
                    "CASE WHEN json_valid(env) THEN (SELECT COUNT(*) FROM json_each(accounts.env) "
                    f"WHERE key IN ({_GEMINI_AUTH_SQL}) AND (type != 'text' OR value != '')) ELSE 0 END AS gemini_auth_unsafe, "
                    "CASE WHEN json_valid(env) THEN json_extract(env,'$.XDG_DATA_HOME') END AS xdg_data, "
                    "CASE WHEN json_valid(env) THEN json_extract(env,'$.XDG_CONFIG_HOME') END AS xdg_config, "
                    "CASE WHEN json_valid(env) THEN json_extract(env,'$.XDG_CACHE_HOME') END AS xdg_cache, "
                    "CASE WHEN json_valid(env) THEN json_extract(env,'$.XDG_STATE_HOME') END AS xdg_state, "
                    "CASE WHEN env IS NULL OR env='' THEN 0 WHEN json_valid(env)=0 THEN 1 ELSE "
                    "(SELECT COUNT(*) FROM json_each(accounts.env) WHERE key NOT IN "
                    "('CODEX_HOME','CLAUDE_CONFIG_DIR','GEMINI_CLI_HOME','GEMINI_FORCE_ENCRYPTED_FILE_STORAGE',"
                    f"'XDG_DATA_HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_STATE_HOME',{_GEMINI_AUTH_SQL})) END AS unknown_env "
                    "FROM accounts")]
                bindings = [dict(row) for row in connection.execute(
                    "SELECT name,workspace,pane_workspace,session_ref,agent,agent_session,account FROM handles")]
            return accounts, bindings
        except sqlite3.Error:
            raise AccountError("ledger_unavailable", "Radio account bindings are unavailable; no launch was attempted.") from None

    def _default_home(self, provider):
        if provider == "codex":
            return _path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        if provider == "claude":
            return _path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        if provider == "gemini":
            return _path(os.environ.get("GEMINI_CLI_HOME") or Path.home()) / ".gemini"
        return _path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")

    def _status_for(self, profile):
        name = profile["id"]
        if name in self._status:
            return deepcopy(self._status[name])
        home = Path(profile["home"])
        credential = home / {"codex": "auth.json", "opencode": "opencode/auth.json",
                             "gemini": "oauth_creds.json", "claude": ".credentials.json"}[profile["provider"]]
        state = "credential_present" if credential.is_file() else "unknown"
        # OpenCode versions may keep credentials in their database instead;
        # absence of auth.json is not evidence that a login is missing.
        if profile["provider"] == "codex" and profile["source"] == "managed" and not credential.is_file():
            state = "not_connected"
        return dict(state=state, email_masked=None, email_domain=None, plan=None,
                    auth_type=None, quota_windows=[], observed_at=None, error=None, balance=None)

    def list(self):
        store = self._store()
        accounts, bindings = self._ledger()
        profiles = []
        seen = set()
        for raw in store["profiles"]:
            if not isinstance(raw, dict):
                raise AccountError("invalid_profile_store")
            profile = self._profile(raw["id"], raw["provider"], raw["home"], raw.get("label"), raw.get("source", "managed"))
            profile["radio_account"] = raw.get("radio_account")
            if profile["id"].lower() in seen:
                raise AccountError("invalid_profile_store")
            if profile["radio_account"]:
                row = next((row for row in accounts if row["name"] == profile["radio_account"]), None)
                if row is None:
                    profile["radio_account"] = None
                elif row["provider"] != profile["provider"] or not row["home"] or not _same_path(row["home"], profile["home"]):
                    profile["configuration_error"] = "radio_profile_changed"
                else:
                    expected = self._profile_env(profile)
                    columns = {"codex_home": "CODEX_HOME", "xdg_data": "XDG_DATA_HOME", "xdg_config": "XDG_CONFIG_HOME",
                               "xdg_cache": "XDG_CACHE_HOME", "xdg_state": "XDG_STATE_HOME",
                               "claude_home": "CLAUDE_CONFIG_DIR", "gemini_home": "GEMINI_CLI_HOME"}
                    mismatch = bool(row["unknown_env"])
                    for column, key in columns.items():
                        if row[column] is not None and (key not in expected or not isinstance(row[column], str)
                                                       or not _same_path(row[column], expected[key])):
                            mismatch = True
                    if profile["source"] == "managed" and profile["provider"] == "opencode":
                        mismatch |= any(row[column] is None for column in ("xdg_config", "xdg_cache", "xdg_state"))
                    # Radio's Gemini home means the config directory; the CLI's
                    # variable means its parent. The explicit override is required.
                    if profile["provider"] == "gemini":
                        mismatch |= row["gemini_home"] is None or row["gemini_storage"] != "false"
                        mismatch |= row["gemini_auth_count"] != len(_GEMINI_BLANK_ENV) or bool(row["gemini_auth_unsafe"])
                    elif row["gemini_storage"] is not None or row["gemini_auth_count"]:
                        mismatch = True
                    profile["custom_environment"] = mismatch
            profiles.append(profile)
            seen.add(profile["id"].lower())
        for row in accounts:
            if row["provider"] not in PROVIDERS or row["name"].lower() in seen:
                continue
            try:
                profile = self._profile(row["name"], row["provider"], row["home"] or self._default_home(row["provider"]), source="radio")
            except AccountError:
                continue
            profile["radio_account"] = row["name"]
            profile["custom_environment"] = bool(row["custom_env"])
            profiles.append(profile)
            seen.add(profile["id"].lower())
        for provider in PROVIDERS:
            home = self._default_home(provider)
            name = provider + "-default"
            if name.lower() not in seen and home.is_dir() and not any(
                    item["provider"] == provider and _same_path(item["home"], home) for item in profiles):
                profiles.append(self._profile(name, provider, home, provider.title() + " · Existing", "system"))
        for profile in profiles:
            profile["status"] = self._status_for(profile)
            profile["bindings"] = []
            for binding in bindings:
                named = binding["account"] and binding["account"] == profile["radio_account"]
                legacy = (not binding["account"] and binding["agent"] == profile["provider"]
                          and _same_path(profile["home"], self._default_home(profile["provider"])))
                if named or legacy:
                    profile["bindings"].append(dict(handle=binding["name"], workspace=binding["pane_workspace"] or binding["workspace"],
                                                    pane=binding["session_ref"].removeprefix("herdr:"), provider=binding["agent"],
                                                    session_id=binding["agent_session"], identity_verified=False,
                                                    evidence="radio_ledger", state="unverified"))
        return {"version": VERSION, "profiles": profiles, "defaults": deepcopy(store["defaults"])}

    def _profile(self, name, provider, home, label=None, source="managed"):
        if provider not in PROVIDERS:
            raise AccountError("unsupported_provider")
        if source not in ("managed", "linked", "system", "radio"):
            raise AccountError("invalid_profile_store")
        return dict(id=_name(name), label=_label(label or name), provider=provider, home=str(_path(home)),
                    source=source, radio_account=None, custom_environment=False, identity_verified=False)

    def get(self, name):
        _name(name)
        found = [profile for profile in self.list()["profiles"] if profile["id"].lower() == name.lower()]
        if len(found) != 1:
            raise AccountError("unknown_profile", "Choose an existing account profile.")
        return found[0]

    def _radio(self, args):
        if self.radio_home is None:
            raise AccountError("radio_context_missing", "Open Accounts from the target Watchtower server.")
        environment = os.environ.copy()
        environment["RADIO_HOME"] = str(self.radio_home)
        try:
            result = subprocess.run(self.radio_command + list(args), env=environment, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12,
                                    creationflags=_quiet_flags(), check=False)
        except (OSError, subprocess.SubprocessError):
            raise AccountError("radio_unavailable", "Radio could not register the profile.") from None
        if result.returncode:
            raise AccountError("radio_registration_failed", "Radio rejected the profile registration; existing accounts were preserved.")

    def _profile_env(self, profile):
        if profile["provider"] == "codex":
            return {"CODEX_HOME": profile["home"]}
        if profile["provider"] == "claude":
            return {"CLAUDE_CONFIG_DIR": profile["home"]}
        root = Path(profile["home"])
        if profile["provider"] == "gemini":
            if root.name != ".gemini":
                raise AccountError("invalid_gemini_home", "Choose the existing .gemini configuration directory.")
            # The optional encrypted store uses a shared keychain entry; keep
            # OAuth in the provider's scoped home instead of that shared entry.
            # Defined empty values also prevent the CLI from loading alternate
            # auth or API gateway settings from a project's .env after launch.
            return {"GEMINI_CLI_HOME": str(root.parent), "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "false",
                    **dict.fromkeys(_GEMINI_BLANK_ENV, "")}
        if profile["source"] == "managed":
            return {"XDG_DATA_HOME": str(root), "XDG_CONFIG_HOME": str(root.parent / "config"),
                    "XDG_CACHE_HOME": str(root.parent / "cache"), "XDG_STATE_HOME": str(root.parent / "state")}
        return {"XDG_DATA_HOME": str(root)}

    def _register(self, profile):
        args = ["account", "add", profile["id"], "--provider", profile["provider"], "--home", profile["home"]]
        for key, value in self._profile_env(profile).items():
            if key != _HOME_ENV[profile["provider"]] or profile["provider"] == "gemini":
                args += ["--env", key + "=" + value]
        self._radio(args)
        profile["radio_account"] = profile["id"]

    def add(self, name, provider, label=None, home=None):
        _name(name)
        if provider not in PROVIDERS:
            raise AccountError("unsupported_provider")
        if self.radio_home is None:
            raise AccountError("radio_context_missing", "Open Accounts from the target Watchtower server.")
        with self._lock():
            existing = self.list()["profiles"]
            if any(profile["id"].lower() == name.lower() for profile in existing):
                raise AccountError("duplicate_profile", "An account with that name already exists.")
            directory = {"codex": "codex", "opencode": "data", "claude": "claude", "gemini": "gemini/.gemini"}[provider]
            target = _path(home) if home else self.home / "state/accounts/profiles" / name / directory
            if any(_same_path(profile["home"], target) for profile in existing):
                raise AccountError("duplicate_home", "That account directory already has a profile.")
            source = "linked" if home else "managed"
            if not home and target.exists():
                raise AccountError("home_exists", "That profile directory already exists; choose another name.")
            if home and not target.is_dir():
                raise AccountError("home_missing", "The existing account directory was not found.")
            profile = self._profile(name, provider, target, label, source)
            self._profile_env(profile)  # Validate provider-specific home mapping before creating state.
            if not home:
                target.mkdir(parents=True)
                if provider == "codex":
                    # Explicit file storage avoids shared keyring login ambiguity.
                    (target / "config.toml").write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
                elif provider == "opencode":
                    for directory in ("config", "cache", "state"):
                        (target.parent / directory).mkdir()
            store = self._store()
            store["profiles"].append({key: profile[key] for key in ("id", "provider", "label", "home", "source", "radio_account")})
            _atomic_json(self.path, store)
            # Preserve an unregistered profile if Radio is unavailable so the
            # next explicit launch can retry registration without a new home.
            self._register(profile)
            store["profiles"][-1]["radio_account"] = profile["radio_account"]
            _atomic_json(self.path, store)
        return self.get(name)

    def ensure_registered(self, name):
        with self._lock():
            profile = self.get(name)
            if profile["radio_account"]:
                return profile
            self._register(profile)
            store = self._store()
            replacement = {key: profile[key] for key in ("id", "provider", "label", "home", "source", "radio_account")}
            previous = next((index for index, item in enumerate(store["profiles"]) if item["id"] == profile["id"]), None)
            if previous is None:
                store["profiles"].append(replacement)
            else:
                store["profiles"][previous] = replacement
            _atomic_json(self.path, store)
        return self.get(name)

    def refresh(self, name):
        profile = self.get(name)
        status = self._status_for(profile)
        if profile.get("custom_environment") or profile.get("configuration_error"):
            status.update(state="unavailable", observed_at=_now(), error="profile_configuration_unverified", quota_windows=[])
        elif profile["provider"] == "codex":
            details = self.reader.read(Path(profile["home"]), force=True)
            status.update(details)
            status["state"] = "connected" if details.get("auth_type") else (
                "not_connected" if details.get("error") == "account_unavailable" else "unavailable")
        elif profile["provider"] == "opencode":
            status = self._status_for({**profile, "id": "__uncached__"})
            status["observed_at"] = _now()
            status["error"] = "provider_status_not_verified"
            try:
                status.update(self._opencode_status(profile))
            except AccountError:
                pass
        else:
            status = self._status_for({**profile, "id": "__uncached__"})
            status.update(observed_at=_now(), error="provider_status_not_verified")
            if profile["provider"] == "claude":
                try:
                    status.update(self._claude_status(profile))
                except AccountError:
                    pass
        self._status[profile["id"]] = status
        return self.get(name)

    def _opencode_status(self, profile):
        """Ask the native CLI for credential presence; never inspect its DB.

        OpenCode 2 returns provider rows with connection metadata. Only a
        boolean presence result for the fixed Zen provider IDs crosses this
        boundary. Connection labels, IDs, auth methods and all other provider
        data are discarded. Presence does not prove valid billing or tokens.
        """
        command = provider_command("opencode")
        if not self._supports_standalone(command):
            raise AccountError("standalone_unavailable")
        plan = self._plan(profile, "status", command + ["auth", "list", "--standalone", "--format", "json"])
        process = None
        try:
            options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           env=plan_environment(plan), cwd=profile["home"])
            if os.name == "nt":
                options["creationflags"] = _quiet_flags()
            else:
                options["start_new_session"] = True
            process = subprocess.Popen(plan["argv"], **options)
            output, _ = process.communicate(timeout=12)
            if process.returncode or len(output) > 1024 * 1024:
                raise AccountError("provider_status_not_verified")
            payload = json.loads(output)
            return self._parse_opencode_status(payload)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise AccountError("provider_status_not_verified") from None
        finally:
            if process is not None:
                _cleanup(process)

    @staticmethod
    def _parse_opencode_status(payload):
        if not isinstance(payload, list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                                               or not isinstance(row.get("connections"), list) for row in payload):
            raise AccountError("provider_status_not_verified")
        present = any(row["id"] in ("opencode", "opencode-zen") and any(isinstance(item, dict) for item in row["connections"])
                      for row in payload)
        return {"state": "credential_present" if present else "not_connected", "error": None,
                "provider": "opencode", "credential_source": "native_auth_list", "balance": None,
                "observed_at": _now(), "identity_verified": False}

    def _claude_status(self, profile):
        """Read the CLI's auth metadata, never its credential file or tokens."""
        plan = self._plan(profile, "status", provider_command("claude") + ["auth", "status", "--json"])
        process = None
        try:
            options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           env=plan_environment(plan), cwd=profile["home"])
            if os.name == "nt":
                options["creationflags"] = _quiet_flags()
            else:
                options["start_new_session"] = True
            process = subprocess.Popen(plan["argv"], **options)
            output, _ = process.communicate(timeout=12)
            if process.returncode not in (0, 1) or len(output) > 1024 * 1024:
                raise AccountError("provider_status_not_verified")
            payload = json.loads(output)
            if not isinstance(payload, dict) or type(payload.get("loggedIn")) is not bool:
                raise AccountError("provider_status_not_verified")
            if process.returncode != (0 if payload["loggedIn"] else 1):
                raise AccountError("provider_status_not_verified")
            return self._parse_claude_status(payload)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise AccountError("provider_status_not_verified") from None
        finally:
            if process is not None:
                _cleanup(process)

    @staticmethod
    def _parse_claude_status(payload):
        if not isinstance(payload, dict) or type(payload.get("loggedIn")) is not bool:
            raise AccountError("provider_status_not_verified")
        present = payload["loggedIn"]
        method = payload.get("authMethod")
        auth_type = method if present and method in ("claude.ai", "api_key", "bedrock", "vertex", "foundry") else None
        plan = payload.get("subscriptionType")
        plan = plan.lower() if present and isinstance(plan, str) and plan.lower() in (
            "free", "pro", "max", "team", "enterprise") else None
        email = payload.get("email") if present else None
        return {"state": "credential_present" if present else "not_connected", "error": None,
                "provider": "claude", "credential_source": "native_auth_status", "auth_type": auth_type,
                "email_masked": _mask_email(email), "email_domain": _email_domain(email), "plan": plan,
                "balance": None, "quota_windows": [], "observed_at": _now(), "identity_verified": False}

    def _supports_standalone(self, command):
        key = tuple(command)
        if key not in self._standalone:
            try:
                result = subprocess.run(command + ["--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, timeout=4, creationflags=_quiet_flags(), check=False)
                self._standalone[key] = b"--standalone" in result.stdout[:65536]
            except (OSError, subprocess.SubprocessError):
                self._standalone[key] = False
        return self._standalone[key]

    def _supports_gemini_home(self, command):
        """Old Gemini CLIs ignore GEMINI_CLI_HOME and would reuse shared auth.

        0.61.0 is the verified baseline for the supported parent-home mapping.
        Never compensate by redirecting HOME/USERPROFILE for project tools.
        """
        key = tuple(command)
        if key not in self._gemini_isolation:
            try:
                result = subprocess.run(command + ["--version"], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
                                        creationflags=_quiet_flags(), check=False)
                match = re.fullmatch(rb"\s*(\d+)\.(\d+)\.(\d+)\s*", result.stdout[:256])
                self._gemini_isolation[key] = bool(result.returncode == 0 and match and
                                                  tuple(map(int, match.groups())) >= _GEMINI_HOME_MIN_VERSION)
            except (OSError, subprocess.SubprocessError):
                self._gemini_isolation[key] = False
        return self._gemini_isolation[key]

    def _require_provider_isolation(self, profile, command):
        if profile["provider"] == "gemini" and not self._supports_gemini_home(command):
            raise AccountError("gemini_update_required",
                               "Update Gemini CLI to 0.61.0 or newer before connecting or launching account profiles.")

    def _plan(self, profile, kind, argv):
        if profile.get("configuration_error"):
            raise AccountError("radio_profile_changed", "Radio's profile home changed; review the existing registration before launching.")
        if profile.get("custom_environment"):
            raise AccountError("custom_environment", "This Radio profile has custom environment overrides; use its existing launcher.")
        unset = list(UNSET_ENV)
        if profile["provider"] == "opencode":
            # An existing native profile uses the user's native companion XDG
            # defaults, never config/cache/state inherited from another managed
            # account pane. Managed profiles explicitly restore their own paths.
            unset += ["XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"]
        return dict(kind=kind, profile_id=profile["id"], provider=profile["provider"], argv=argv,
                    env=self._profile_env(profile), unset_env=unset, cwd=None,
                    interactive=True, identity_verified=False)

    def connect_plan(self, name):
        profile = self.get(name)
        command = provider_command(profile["provider"])
        self._require_provider_isolation(profile, command)
        if profile["provider"] == "codex":
            args = ["login"]
            if profile["source"] == "managed":
                args += ["-c", 'cli_auth_credentials_store="file"']
        elif profile["provider"] == "claude":
            args = ["auth", "login", "--claudeai"]
        elif profile["provider"] == "gemini":
            # Gemini's native interactive CLI owns the Google sign-in selector.
            args = []
        else:
            args = ["auth", "login", "opencode"]
            if not self._supports_standalone(command):
                raise AccountError("standalone_unavailable", "This OpenCode CLI cannot confirm private-server support; update it before connecting profiles.")
            args.append("--standalone")
        plan = self._plan(profile, "connect", command + args)
        if profile["provider"] in ("gemini", "claude"):
            # Login must not inherit a project's .env or settings overrides.
            plan["cwd"] = profile["home"]
        return plan

    def models(self, name):
        """List a selected profile's catalog without starting an agent or login."""
        try:
            from . import models as catalogs
        except ImportError:
            import models as catalogs
        profile = self.get(name)
        plan = self._plan(profile, "models", [])
        provider = profile["provider"]
        key = (provider, profile["id"], profile["home"], tuple(sorted(plan["env"].items())))
        if provider == "codex":
            cache = Path(profile["home"]) / "models_cache.json"
            try:
                stamp = (cache.stat().st_mtime_ns, cache.stat().st_size)
            except OSError:
                stamp = None
            return catalogs.CACHE.read((*key, stamp), lambda: catalogs.codex_catalog(profile))
        if provider == "opencode":
            def discover():
                try:
                    command = provider_command(provider)
                    if not self._supports_standalone(command):
                        return catalogs.unavailable(provider, "Update OpenCode CLI to list models in an isolated account context.")
                    return catalogs.opencode_catalog(command, plan_environment(plan), _cleanup)
                except AccountError:
                    return catalogs.unavailable(provider)
            return catalogs.CACHE.read(key, discover)
        return catalogs.unavailable(provider, "Use Provider default; model discovery is not available for this tool yet.")

    def launch_plan(self, name, handle, workspace=None, model=None):
        _name(handle)
        if not isinstance(workspace, str) or not WORKSPACE_RE.fullmatch(workspace):
            raise AccountError("workspace_required", "Choose the workspace for the new agent.")
        profile = self.get(name)
        _, bindings = self._ledger()
        if any(row["name"].lower() == handle.lower() and workspace in (row["workspace"], row["pane_workspace"]) for row in bindings):
            raise AccountError("handle_in_use", "That handle already belongs to this workspace; choose a new handle.")
        if not profile["radio_account"]:
            raise AccountError("profile_not_registered", "Register this existing account before launching a new agent.")
        command = provider_command(profile["provider"])
        self._require_provider_isolation(profile, command)
        if profile["provider"] == "opencode" and not self._supports_standalone(command):
            raise AccountError("standalone_unavailable", "This OpenCode CLI cannot confirm private-server support; update it before launching profiles.")
        args = ["join", handle, "--new", "--account", profile["radio_account"], "--workspace-frequency"]
        if model is not None:
            try:
                from .models import valid_model
            except ImportError:
                from models import valid_model
            if not valid_model(profile["provider"], model):
                raise AccountError("invalid_model")
            if profile["provider"] == "opencode":
                args += ["--model", model]
        plan = self._plan(profile, "launch", self.radio_command + args)
        plan["radio_source"] = str(Path(__file__).resolve().parent.parent / "radio/vendor/AgentRadio/bin/radio")
        plan["workspace"] = workspace
        plan["handle"] = handle
        plan["model"] = model
        plan["provider_argv"] = command
        plan["provider_extra_args"] = ["--standalone"] if profile["provider"] == "opencode" and self._supports_standalone(command) else []
        return plan

    def set_default(self, name):
        with self._lock():
            profile = self.get(name)
            store = self._store()
            store["defaults"]["default_profile"] = profile["id"]
            _atomic_json(self.path, store)
        return deepcopy(store["defaults"])

    def default_profile(self, provider=None):
        state = self.list()
        selected = state["defaults"].get("default_profile")
        return next((item for item in state["profiles"] if item["id"] == selected and (not provider or item["provider"] == provider)), None)


AccountManager = AccountService


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home")
    parser.add_argument("--radio-home")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for command in ("status", "connect-plan", "default", "register"):
        sub.add_parser(command).add_argument("name")
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--provider", required=True, choices=PROVIDERS)
    add.add_argument("--label")
    add.add_argument("--existing-home")
    launch = sub.add_parser("launch-plan")
    launch.add_argument("name")
    launch.add_argument("--handle", required=True)
    launch.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)
    try:
        service = AccountService(args.home, args.radio_home)
        if args.command == "list":
            result = service.list()
        elif args.command == "add":
            result = service.add(args.name, args.provider, args.label, args.existing_home)
        elif args.command == "status":
            result = service.refresh(args.name)
        elif args.command == "connect-plan":
            result = service.connect_plan(args.name)
        elif args.command == "launch-plan":
            result = service.launch_plan(args.name, args.handle, args.workspace)
        elif args.command == "register":
            result = service.ensure_registered(args.name)
        else:
            result = service.set_default(args.name)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except AccountError as error:
        print(json.dumps({"error": error.code, "message": error.message}))
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"error": "unavailable", "message": "Accounts operation could not be completed."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
