"""Local review artifacts and Radio routing; never a deploy or database approval."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

ROOT = Path(os.environ.get("WATCHTOWER_HOME", Path.home() / ".watchtower")) / "state" / "teams"
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
QUALIFIED = re.compile(r"(w[0-9]+):([A-Za-z0-9][A-Za-z0-9_-]{0,63})\Z")
EXITS = {"PASS": 0, "PENDING_LOCAL": 2, "PENDING_GLOBAL": 2,
         "MISSING": 2, "STALE": 3, "BLOCKED": 4, "CHANGES_REQUIRED": 5}


class ReviewError(Exception):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def safe(value):
    if not isinstance(value, str) or not SAFE.fullmatch(value):
        raise ReviewError("Team and review IDs must contain 1-64 letters, digits, underscores or hyphens.")
    return value


def read_json(path):
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (OSError, ValueError):
        raise ReviewError("Missing or invalid JSON artifact: " + str(path)) from None


def immutable(path, content):
    """An existing artifact must match byte for byte; never overwrite it."""
    if path.resolve() != path.absolute():
        raise ReviewError("Review output paths must not redirect through symlinks.")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ReviewError("Immutable artifact already exists with different content: " + str(path)) from None


def write_json(path, value):
    if path.resolve() != path.absolute():
        raise ReviewError("Review output paths must not redirect through symlinks.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    try:
        temporary.write_bytes(canonical(value))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(directory):
    if directory.resolve() != directory.absolute():
        raise ReviewError("Review output paths must not redirect through symlinks.")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".review.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ReviewError("Review operation already in progress; inspect a leftover lock manually after a crash.") from None
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


def run(argv, *, env=None, timeout=30):
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, check=True,
                              env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        raise ReviewError("External command unavailable or unsuccessful; no permissions were changed.") from None


def file_hash(path):
    try:
        with Path(path).open("rb") as stream:
            sha = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(chunk)
            return sha.hexdigest()
    except OSError:
        raise ReviewError("Review artifact is unreadable: " + str(path)) from None


def absolute_file(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ReviewError("Artifact and report paths must be absolute.")
    path = Path(value).resolve()
    if not path.is_file():
        raise ReviewError("Expected an existing file: " + str(path))
    return path


def git(repo, *args):
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return run(["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                "-C", str(repo), *args], env=env).stdout


def repo_snapshot(repo):
    root = git(repo, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    if Path(root).resolve() != repo:
        raise ReviewError("Repository paths must identify the worktree root.")
    for entry in git(repo, "ls-files", "-v", "-z").split(b"\0"):
        if entry and (entry[:1].islower() or entry[:1] == b"S"):
            raise ReviewError("Repository has assume-unchanged or skip-worktree files; use an explicit artifact scope instead.")
    for entry in git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if entry.startswith((b"160000 ", b"120000 ")):
            raise ReviewError("Repositories with submodules or tracked symlinks need an explicit artifact or separate repository scope.")
    head = git(repo, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    result = {"head": head}
    for key, args in (
        ("status", ["status", "--porcelain=v1", "-z", "--untracked-files=all"]),
        ("working_diff", ["diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD", "--"]),
        ("index_diff", ["diff", "--no-ext-diff", "--no-textconv", "--binary", "--cached", "HEAD", "--"]),
    ):
        result[key] = hashlib.sha256(git(repo, *args)).hexdigest()
    untracked = []
    for raw in git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if not raw:
            continue
        relative = os.fsdecode(raw)
        path = repo / relative
        if path.is_symlink() or not path.resolve().is_relative_to(repo):
            raise ReviewError("Untracked symlinks or paths escaping the repository need an explicit, separate review scope.")
        untracked.append({"path": relative, "sha256": file_hash(path)})
    result["untracked"] = sorted(untracked, key=lambda item: item["path"])
    return result


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(name + " must be a nonempty string.")
    return value.strip()


def strings(value, name, *, required=False):
    if not isinstance(value, list) or (required and not value):
        raise ReviewError(name + " must be a list" + (" with at least one item." if required else "."))
    return [text(item, name) for item in value]


class ReviewSystem:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()

    def team(self, name):
        path = self.root / safe(name)
        if path.resolve() != path:
            raise ReviewError("Team directories must not redirect outside their configured path.")
        return read_json(path / "config.json")

    def global_team(self, qualified):
        match = QUALIFIED.fullmatch(qualified) if isinstance(qualified, str) else None
        if not match:
            raise ReviewError("global_reviewer must be an explicit workspace:handle address.")
        workspace, role = match.groups()
        matches = []
        for path in sorted(self.root.glob("*/config.json")):
            config = read_json(path)
            if config.get("workspace_id") == workspace and role in config.get("roles", {}):
                matches.append(path.parent.name)
        if len(matches) != 1:
            raise ReviewError("Global reviewer must resolve to exactly one configured team.")
        return matches[0], role

    def policy(self, config):
        review = config.get("review")
        if not isinstance(review, dict):
            raise ReviewError("Team review routing has not been configured.")
        policy = {key: review.get(key) for key in ("local_reviewer", "lead", "global_reviewer")}
        if type(review.get("global_required", False)) is not bool:
            raise ReviewError("review.global_required must be a boolean.")
        policy["global_required"] = review.get("global_required", False)
        policy["global_mode"] = review.get("global_mode", "automatic")
        if policy["global_mode"] not in ("automatic", "on_demand"):
            raise ReviewError("review.global_mode must be automatic or on_demand.")
        for key in ("local_reviewer", "lead"):
            if policy[key] not in config.get("roles", {}):
                raise ReviewError("Review routing must reference configured local roles.")
            safe(policy[key])
        if policy["local_reviewer"] == policy["lead"]:
            raise ReviewError("Lead and independent local reviewer must be different roles.")
        global_team = None
        if policy["global_reviewer"] is not None:
            global_team, _ = self.global_team(policy["global_reviewer"])
        policy.update(workspace_id=config["workspace_id"], global_team=global_team)
        return policy

    def identity(self, config):
        """Ambient pane identity is cross-checked; caller-supplied role claims are never accepted."""
        pane_id = os.environ.get("HERDR_PANE_ID", "")
        workspace = config["workspace_id"]
        if (os.environ.get("HERDR_ENV") != "1" or not pane_id
                or os.environ.get("HERDR_WORKSPACE_ID") != workspace):
            raise ReviewError("BLOCKED: run from this team's live Herdr pane; manual reports cannot pass the gate.")
        effective_home = (Path(os.environ["RADIO_HOME"]).expanduser() if os.environ.get("RADIO_HOME")
                          else Path.home() / ".local/share/herdr-radio")
        if effective_home.resolve() != Path(config["radio_home"]).resolve():
            raise ReviewError("BLOCKED: inherited Radio state directory does not match the configured ledger.")
        try:
            pane = json.loads(run([config["herdr"], "pane", "get", pane_id]).stdout)["result"]["pane"]
            database = (Path(config["radio_home"]) / "radio.db").resolve()
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute("SELECT * FROM handles WHERE session_ref=?", ("herdr:" + pane_id,)).fetchall()
            if len(rows) != 1:
                raise ValueError()
            row = dict(rows[0])
            role = row["name"]
            session = (pane.get("agent_session") or {}).get("value")
            expected_role = config["roles"].get(role)
            provider = expected_role.get("provider", "codex") if isinstance(expected_role, dict) else None
            if (not expected_role or pane.get("pane_id") != pane_id or pane.get("workspace_id") != workspace
                    or provider not in ("codex", "opencode")
                    or pane.get("agent") != provider or row.get("agent") != provider
                    or row.get("workspace") != workspace
                    or (row.get("pane_workspace") or row["workspace"]) != workspace
                    or not session or row.get("agent_session") != session
                    or pane.get("label") != role
                    or not pane.get("cwd") or Path(pane["cwd"]).resolve() != Path(expected_role["cwd"]).resolve()
                    or (provider == "codex" and os.environ.get("CODEX_THREAD_ID", session) != session)
                    or os.environ.get("RADIO_HANDLE", role) not in (role, workspace + ":" + role)
                    or os.environ.get("RADIO_JOINED_SCOPE", workspace) != workspace):
                raise ValueError()
            # OpenCode's TUI integration reports its selected root session to
            # Herdr; Radio backfills that exact session. Never substitute an
            # invented environment variable or accept a missing session hook.
            return {"role": role, "workspace": workspace, "pane": pane_id, "session": session,
                    "provider": provider}
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ReviewError):
            raise ReviewError("BLOCKED: live Herdr/Radio reviewer identity could not be verified; keep a manual report and ask the lead to restore access.") from None

    def normalize(self, source):
        allowed = {"task", "summary", "acceptance_criteria", "artifacts", "repos", "shared_impact", "notes",
                   "reviewer_contributed", "global_review_requested"}
        if not isinstance(source, dict) or set(source) - allowed:
            raise ReviewError("Request contains unsupported fields; reviewer identity and routing come only from team config.")
        result = {"task": text(source.get("task"), "task"), "summary": text(source.get("summary"), "summary"),
                  "acceptance_criteria": strings(source.get("acceptance_criteria"), "acceptance_criteria", required=True),
                  "shared_impact": strings(source.get("shared_impact", []), "shared_impact"), "artifacts": [], "repos": []}
        if type(source.get("reviewer_contributed", False)) is not bool:
            raise ReviewError("reviewer_contributed must be a boolean.")
        result["reviewer_contributed"] = source.get("reviewer_contributed", False)
        if type(source.get("global_review_requested", False)) is not bool:
            raise ReviewError("global_review_requested must be a boolean.")
        result["global_review_requested"] = source.get("global_review_requested", False)
        if "notes" in source:
            result["notes"] = text(source["notes"], "notes")
        for kind in ("artifacts", "repos"):
            if not isinstance(source.get(kind, []), list):
                raise ReviewError(kind + " must be a list.")
        for item in source.get("artifacts", []):
            if not isinstance(item, dict) or set(item) - {"path", "sha256"}:
                raise ReviewError("Artifact fields must be path and optional sha256.")
            path = absolute_file(item.get("path"))
            sha = file_hash(path)
            if item.get("sha256", sha) != sha:
                raise ReviewError("Provided artifact SHA256 does not match its content.")
            result["artifacts"].append({"path": str(path), "sha256": sha})
        for item in source.get("repos", []):
            if not isinstance(item, dict) or set(item) - {"path", "base", "head"}:
                raise ReviewError("Repository fields must be path and optional base/head.")
            if not isinstance(item.get("path"), str) or not Path(item["path"]).is_absolute():
                raise ReviewError("Repository paths must be absolute.")
            path = Path(item["path"]).resolve()
            snapshot = repo_snapshot(path)
            repo = {"path": str(path), "snapshot": snapshot}
            for key in ("base", "head"):
                if key in item:
                    ref = text(item[key], key)
                    commit = git(path, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode("ascii").strip()
                    if key == "head" and commit != snapshot["head"]:
                        raise ReviewError("Provided HEAD is not the worktree's current HEAD.")
                    repo[key] = commit
            result["repos"].append(repo)
        if not result["artifacts"] and not result["repos"]:
            raise ReviewError("Review requires at least one file artifact or repository snapshot.")
        for key in ("artifacts", "repos"):
            result[key].sort(key=lambda item: item["path"])
            if len({item["path"] for item in result[key]}) != len(result[key]):
                raise ReviewError("Duplicate review scope paths are not accepted.")
        return result

    def directory(self, team, review_id):
        path = self.root / safe(team) / "reviews" / safe(review_id)
        if path.resolve() != path:
            raise ReviewError("Review directories must not be redirected by symlinks.")
        return path

    def load(self, team, review_id, requested=None):
        directory = self.directory(team, review_id)
        pointer = read_json(directory / "request.json")
        sha = pointer.get("digest")
        if not isinstance(sha, str) or not DIGEST.fullmatch(sha):
            raise ReviewError("Invalid request pointer.")
        if requested is not None and requested != sha:
            raise ReviewError("Decision digest is not the current request revision.")
        path = directory / "revisions" / sha / "request.json"
        envelope = read_json(path)
        payload = envelope.get("payload")
        if (not isinstance(payload, dict) or envelope.get("digest") != sha or digest(payload) != sha
                or payload.get("team") != team or payload.get("id") != review_id):
            raise ReviewError("Request digest or scope verification failed.")
        return envelope, path

    def fresh(self, envelope):
        payload = envelope["payload"]
        current_policy = self.policy(self.team(payload["team"]))
        # Immutable pre-mode requests retain their original routing and automatic
        # escalation policy. A new default must not rewrite historical evidence.
        if "global_mode" not in payload["policy"]:
            current_policy.pop("global_mode")
        if current_policy != payload["policy"]:
            raise ReviewError("Review routing changed after submission.")
        for item in payload["request"]["artifacts"]:
            if file_hash(item["path"]) != item["sha256"]:
                raise ReviewError("Reviewed artifact content changed.")
        for item in payload["request"]["repos"]:
            if repo_snapshot(Path(item["path"])) != item["snapshot"]:
                raise ReviewError("Reviewed repository state changed.")

    def decision_path(self, envelope, stage):
        payload, sha = envelope["payload"], envelope["digest"]
        if stage == "local":
            return self.directory(payload["team"], payload["id"]) / "revisions" / sha / "local" / "decision.json"
        if not payload["policy"]["global_team"]:
            raise ReviewError("Independent global review was requested but no reviewer is configured.")
        return (self.directory(payload["policy"]["global_team"], payload["team"])
                / payload["id"] / sha / "global" / "decision.json")

    def decision(self, envelope, stage):
        if stage == "global" and not envelope["payload"]["policy"]["global_team"]:
            return None
        path = self.decision_path(envelope, stage)
        if not path.exists():
            return None
        decision = read_json(path)
        payload = envelope["payload"]
        expected = (payload["policy"]["workspace_id"] + ":" + payload["policy"]["local_reviewer"]
                    if stage == "local" else payload["policy"]["global_reviewer"])
        actor = decision.get("actor", {})
        if (decision.get("request_digest") != envelope["digest"] or decision.get("stage") != stage
                or decision.get("team") != payload["team"] or decision.get("id") != payload["id"]
                or decision.get("decision") not in ("PASS", "CHANGES_REQUIRED", "BLOCKED")
                or type(decision.get("require_global")) is not bool
                or actor.get("workspace", "") + ":" + actor.get("role", "") != expected
                or not actor.get("session") or not actor.get("pane")
                or file_hash(path.parent / "report.txt") != decision.get("report_sha256")):
            raise ReviewError("Decision artifact failed scope, reviewer or report verification.")
        return decision

    def needs_global(self, envelope, local=None):
        payload = envelope["payload"]
        request = payload["request"]
        mode = payload["policy"].get("global_mode", "automatic")
        if mode not in ("automatic", "on_demand"):
            raise ReviewError("Unknown recorded global review mode.")
        requested = request.get("global_review_requested", False)
        if type(requested) is not bool:
            raise ReviewError("global_review_requested must be a boolean.")
        explicit = (payload["policy"]["global_required"] or requested
                    or (local and local.get("require_global")))
        return bool(explicit or (mode == "automatic" and (
            request["shared_impact"] or len(request["repos"]) > 1 or request["reviewer_contributed"])))

    def require_independent_pass(self, envelope, local):
        request = envelope["payload"]["request"]
        contributed = (request.get("reviewer_contributed", False)
                       or "reviewer_contributed" in request.get("shared_impact", []))
        if contributed and not self.needs_global(envelope, local):
            raise ReviewError("The local reviewer contributed to this deliverable. Request global review explicitly "
                              "or submit a new revision with an independently assigned local reviewer.")

    def notify(self, config, directory, key, recipient, message, reference, *, reply_required):
        path = directory / "notifications" / (key + ".json")
        if path.exists():
            return read_json(path)["state"]
        attempt = {"state": "unknown", "recipient": recipient, "reference": str(reference.resolve()), "attempted_at": now()}
        # Reserve BEFORE the Radio call: a timeout may follow a successful enqueue.
        immutable(path, canonical(attempt))
        argv = [sys.executable, config["radio_entrypoint"], "pm", recipient, message,
                "--ref", str(reference.resolve())]
        if reply_required:
            argv.append("--reply-required")
        try:
            run(argv)
        except ReviewError:
            return "unknown"
        attempt["state"] = "sent"
        write_json(path, attempt)
        return "sent"

    def submit(self, team, review_id, request_path):
        config = self.team(team)
        actor = self.identity(config)
        payload = {"schema": 1, "team": safe(team), "id": safe(review_id),
                   "request": self.normalize(read_json(absolute_file(str(request_path)))), "policy": self.policy(config)}
        if payload["request"]["global_review_requested"] and actor["role"] != payload["policy"]["lead"]:
            raise ReviewError("Only the configured lead can submit global_review_requested=true; "
                              "the local reviewer can use --require-global.")
        sha = digest(payload)
        directory = self.directory(team, review_id)
        with locked(directory):
            if self.identity(config) != actor:
                raise ReviewError("Caller identity changed during request preparation.")
            if (directory / "request.json").exists():
                previous, _ = self.load(team, review_id)
                if previous["digest"] != sha:
                    # A changed source snapshot is expected here; validate the
                    # immutable predecessor decisions, not its old source freshness.
                    local = self.decision(previous, "local")
                    global_decision = self.decision(previous, "global")
                    global_complete = (local and local["decision"] == "PASS"
                                       and global_decision and global_decision["decision"] == "PASS")
                    if (self.needs_global(previous, local) and not global_complete
                            and not self.needs_global({"payload": payload})):
                        raise ReviewError("Unresolved global review from the previous revision must remain required. "
                                          "Have the configured lead submit global_review_requested=true.")
            revision = directory / "revisions" / sha
            path = revision / "request.json"
            if path.exists():
                existing = read_json(path)
                if existing.get("payload") != payload or existing.get("digest") != sha:
                    raise ReviewError("Existing revision content does not match its digest.")
            else:
                immutable(path, canonical({"digest": sha, "payload": payload, "submitted_at": now(), "submitted_by": actor}))
            write_json(directory / "request.json", {"digest": sha, "request": str(path.resolve())})
            delivery = self.notify(config, revision, "submit", payload["policy"]["local_reviewer"],
                                   "REVIEW_REQUEST " + team + "/" + review_id + " digest=" + sha,
                                   path, reply_required=True)
        return {"state": "SUBMITTED", "digest": sha, "request": str(path.resolve()), "delivery": delivery}

    def result(self, team, review_id, sha, choice, report_path, require_global=False):
        if not isinstance(sha, str) or not DIGEST.fullmatch(sha) or choice not in ("PASS", "CHANGES_REQUIRED", "BLOCKED"):
            raise ReviewError("A full request digest and valid decision are required.")
        if type(require_global) is not bool:
            raise ReviewError("require_global must be a boolean flag.")
        config = self.team(team)
        envelope, request_path = self.load(team, review_id, sha)
        policy = envelope["payload"]["policy"]
        current_workspace = os.environ.get("HERDR_WORKSPACE_ID")
        stage = "local" if current_workspace == config["workspace_id"] else "global"
        acting_config = config if stage == "local" else self.team(policy["global_team"])
        actor = self.identity(acting_config)
        expected = policy["local_reviewer"] if stage == "local" else self.global_team(policy["global_reviewer"])[1]
        submitter = envelope.get("submitted_by", {})
        self_review = (stage == "local" and actor["role"] == submitter.get("role")
                       and actor["workspace"] == submitter.get("workspace"))
        if actor["role"] != expected or self_review:
            raise ReviewError("Only the configured independent reviewer can record this stage.")
        self.fresh(envelope)
        if stage == "local" and choice == "PASS":
            self.require_independent_pass(envelope, {"require_global": require_global})
        if stage == "global":
            if require_global:
                raise ReviewError("Only the local reviewer can request escalation with --require-global.")
            local = self.decision(envelope, "local")
            if not local or local["decision"] != "PASS" or not self.needs_global(envelope, local):
                raise ReviewError("Global review requires an escalation reason and a current local PASS.")
        report = absolute_file(str(report_path)).read_bytes()
        if not report.strip():
            raise ReviewError("Reviewer report must not be empty.")
        decision_path = self.decision_path(envelope, stage)
        with locked(decision_path.parent):
            # Revalidate after acquiring the stage lock; old revisions cannot win a race.
            self.load(team, review_id, sha)
            self.fresh(envelope)
            if self.identity(acting_config) != actor:
                raise ReviewError("Reviewer identity changed during decision preparation.")
            decision = {"team": team, "id": review_id, "request_digest": sha, "stage": stage,
                        "decision": choice, "actor": actor, "report_sha256": hashlib.sha256(report).hexdigest(),
                        "require_global": require_global}
            if decision_path.exists():
                existing = self.decision(envelope, stage)
                if any(existing.get(key) != value for key, value in decision.items()):
                    raise ReviewError("A different decision already exists; submit a new request revision.")
            else:
                immutable(decision_path.parent / "report.txt", report)
                immutable(decision_path, canonical({**decision, "recorded_at": now()}))
            forward = stage == "local" and choice == "PASS" and self.needs_global(envelope, decision)
            missing_route = forward and not policy["global_reviewer"]
            if missing_route:
                forward = False
            recipient = policy["global_reviewer"] if forward else policy["workspace_id"] + ":" + policy["lead"]
            reference = request_path if forward else decision_path
            delivery = self.notify(acting_config, decision_path.parent, "decision", recipient,
                                   ("REVIEW_BLOCKED no global reviewer configured " if missing_route else
                                    "GLOBAL_REVIEW_REQUEST " if forward else "REVIEW_RESULT ")
                                   + team + "/" + review_id + " digest=" + sha + " " + choice,
                                   reference, reply_required=forward)
        result = self.status(team, review_id, sha)
        result["delivery"] = delivery
        return result

    def status(self, team, review_id, requested=None):
        pointer = self.directory(team, review_id) / "request.json"
        if not pointer.exists():
            return {"state": "MISSING", "team": team, "id": review_id}
        try:
            envelope, path = self.load(team, review_id)
            sha = envelope["digest"]
            if requested is not None and requested != sha:
                return {"state": "STALE", "digest": sha, "reason": "Gate digest is not the current request revision."}
            try:
                self.fresh(envelope)
            except ReviewError as error:
                return {"state": "STALE", "digest": sha, "reason": str(error)}
            local = self.decision(envelope, "local")
            state = "PENDING_LOCAL" if not local else local["decision"]
            global_decision = None
            if state == "PASS":
                self.require_independent_pass(envelope, local)
            if state == "PASS" and self.needs_global(envelope, local):
                if not envelope["payload"]["policy"]["global_team"]:
                    raise ReviewError("Independent global review was requested but no reviewer is configured.")
                global_decision = self.decision(envelope, "global")
                state = global_decision["decision"] if global_decision else "PENDING_GLOBAL"
            deliveries = {}
            notifications = [("submit", path.parent / "notifications/submit.json"),
                             ("local", self.decision_path(envelope, "local").parent / "notifications/decision.json")]
            if envelope["payload"]["policy"]["global_team"]:
                notifications.append(("global", self.decision_path(envelope, "global").parent / "notifications/decision.json"))
            for name, notification in notifications:
                if notification.exists():
                    deliveries[name] = read_json(notification).get("state", "unknown")
            return {"state": state, "team": team, "id": review_id, "digest": sha,
                    "request": str(path.resolve()), "local": local["decision"] if local else None,
                    "global": global_decision["decision"] if global_decision else None, "notifications": deliveries,
                    "scope": "Local review evidence only; PASS grants no database-write, deployment or publication permission."}
        except ReviewError as error:
            return {"state": "BLOCKED", "reason": str(error)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("submit", "result", "status", "gate"):
        command = commands.add_parser(name)
        command.add_argument("--team", required=True)
        command.add_argument("--id", required=True)
        if name == "submit":
            command.add_argument("--request", type=Path, required=True)
        elif name == "result":
            command.add_argument("--digest", required=True)
            command.add_argument("--decision", choices=("PASS", "CHANGES_REQUIRED", "BLOCKED"), required=True)
            command.add_argument("--report", type=Path, required=True)
            command.add_argument("--require-global", action="store_true")
        elif name in ("status", "gate"):
            command.add_argument("--digest", required=name == "gate")
    args = parser.parse_args(argv)
    system = ReviewSystem()
    try:
        if args.command == "submit":
            result = system.submit(args.team, args.id, args.request)
        elif args.command == "result":
            result = system.result(args.team, args.id, args.digest, args.decision, args.report, args.require_global)
        else:
            result = system.status(args.team, args.id, args.digest)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["state"] == "SUBMITTED" else EXITS[result["state"]]
    except (ReviewError, OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"state": "BLOCKED", "reason": str(error)}, ensure_ascii=False))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
