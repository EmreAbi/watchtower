"""Task records and completion checks for template teams, invoked by their agents.

No background scheduler and no product task starts on workspace creation.
All mutations verify the calling live Radio/Watchtower identity. Review records
prove a decision on a snapshot, not the semantic correctness of that decision.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from . import review_engine as review
except ImportError:
    import review_engine as review


class Workflow:
    def __init__(self, team_dir, engine=None):
        path = Path(team_dir)
        if not path.is_absolute() or path.resolve() != path.absolute():
            raise review.ReviewError("Use the team's absolute, non-redirected metadata directory.")
        self.directory = path.resolve()
        self.team = review.safe(path.name)
        self.engine = engine or review.ReviewSystem(path.parent)

    def config(self):
        return self.engine.team(self.team)

    def task_path(self, task_id):
        return self.directory / "work-items" / review.safe(task_id) / "task.json"

    def actor(self, *, owner=False):
        config = self.config()
        actor = self.engine.identity(config)
        if owner and actor["role"] != config["review"]["lead"]:
            raise review.ReviewError("Only the configured task owner can start or complete a task.")
        return actor

    def begin(self, task_id, request_path):
        actor = self.actor(owner=True)
        request = review.read_json(review.absolute_file(str(request_path)))
        if set(request) - {"task", "acceptance_criteria"}:
            raise review.ReviewError("Task setup accepts only task and acceptance_criteria.")
        scope = {"task": review.text(request.get("task"), "task"),
                 "acceptance_criteria": review.strings(request.get("acceptance_criteria"),
                                                       "acceptance_criteria", required=True)}
        path = self.task_path(task_id)
        with review.locked(self.directory / "work-items"):
            active_path = self.directory / "work-items" / "active.json"
            if active_path.exists():
                active = review.read_json(active_path).get("id")
                if active != task_id:
                    old = review.read_json(self.task_path(active))
                    if not old.get("completed_at"):
                        raise review.ReviewError("This team already has an active task. Complete it before starting another.")
            if path.exists():
                existing = review.read_json(path)
                if existing["scope"] != scope or existing.get("completed_at"):
                    raise review.ReviewError("Task ID already used. Use a new task ID for a different request.")
            else:
                review.write_json(path, {"schema": 1, "id": task_id, "scope": scope,
                                        "started_at": review.now(), "started_by": actor})
            review.write_json(active_path, {"id": task_id})
            config = self.config()
            delivery = None
            if "worker" in config["roles"]:
                delivery = self.engine.notify(config, path.parent, "assignment", "worker",
                    "TASK " + self.team + "/" + task_id + "; implement the recorded scope, then submit evidence for review.",
                    path, reply_required=True)
        return {"state": "WORKING", "id": task_id, "task": str(path), "delivery": delivery}

    def submit(self, task_id, request_path):
        actor = self.actor()
        config = self.config()
        if not config["review"]["required"]:
            raise review.ReviewError("This template has no independent review. Use complete with an outcome summary.")
        if actor["role"] == config["review"]["local_reviewer"]:
            raise review.ReviewError("The independent reviewer cannot submit the deliverable.")
        path = self.task_path(task_id)
        with review.locked(path.parent):
            task = review.read_json(path)
            if task.get("completed_at"):
                raise review.ReviewError("Completed tasks are not reopened; start a new task.")
            request = review.read_json(review.absolute_file(str(request_path)))
            for key in ("task", "acceptance_criteria"):
                if request.get(key) != task["scope"][key]:
                    raise review.ReviewError("Submitted task and acceptance criteria must match the recorded scope.")
            result = self.engine.submit(self.team, task_id, request_path)
        return result

    def result(self, task_id, digest, decision, report, require_global=False):
        with review.locked(self.task_path(task_id).parent):
            task = review.read_json(self.task_path(task_id))
            if task.get("completed_at"):
                raise review.ReviewError("Completed tasks cannot receive new review decisions.")
            return self.engine.result(self.team, task_id, digest, decision, report, require_global)

    def status(self, task_id):
        path = self.task_path(task_id)
        if not path.exists():
            return {"state": "MISSING", "id": task_id}
        task = review.read_json(path)
        if not self.config()["review"]["required"]:
            return {"state": "COMPLETED" if task.get("completed_at") else "WORKING",
                    "id": task_id, "independent_review": False, "task": str(path)}
        gate = self.engine.status(self.team, task_id, task.get("completed_digest"))
        state = gate["state"]
        if state == "MISSING":
            state = "WORKING"
        elif state == "PASS":
            state = "COMPLETED" if task.get("completed_at") else "READY_TO_COMPLETE"
        return {"state": state, "id": task_id, "gate": gate, "task": str(path)}

    def complete(self, task_id, digest, summary):
        actor = self.actor(owner=True)
        summary = review.text(summary, "summary")
        path = self.task_path(task_id)
        with review.locked(path.parent):
            task = review.read_json(path)
            required = self.config()["review"]["required"]
            if required:
                if not isinstance(digest, str) or not review.DIGEST.fullmatch(digest):
                    raise review.ReviewError("Completion needs the full reviewed request digest.")
                gate = self.engine.status(self.team, task_id, digest)
                if gate["state"] != "PASS":
                    raise review.ReviewError("Completion blocked: " + gate["state"] + ". " + gate.get("reason", ""))
            elif digest is not None:
                raise review.ReviewError("Quick Task completion does not take a review digest.")
            if task.get("completed_at"):
                if task.get("completed_digest") != digest or task.get("summary") != summary:
                    raise review.ReviewError("A different completion already exists.")
            else:
                task.update(completed_at=review.now(), completed_by=actor, completed_digest=digest,
                            summary=summary, independent_review=required)
                review.write_json(path, task)
        return self.status(task_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("begin", "submit", "result", "status", "complete"):
        command = commands.add_parser(name)
        command.add_argument("--team-dir", type=Path, required=True)
        command.add_argument("--id", required=True)
        if name in ("begin", "submit"):
            command.add_argument("--request", type=Path, required=True)
        if name in ("result", "complete"):
            command.add_argument("--digest", required=name == "result")
        if name == "result":
            command.add_argument("--decision", required=True, choices=("PASS", "CHANGES_REQUIRED", "BLOCKED"))
            command.add_argument("--report", type=Path, required=True)
            command.add_argument("--require-global", action="store_true")
        if name == "complete":
            command.add_argument("--summary", required=True)
    args = parser.parse_args(argv)
    try:
        flow = Workflow(args.team_dir)
        if args.command in ("begin", "submit"):
            result = getattr(flow, args.command)(args.id, args.request)
        elif args.command == "result":
            result = flow.result(args.id, args.digest, args.decision, args.report, args.require_global)
        elif args.command == "complete":
            result = flow.complete(args.id, args.digest, args.summary)
        else:
            result = flow.status(args.id)
        print(json.dumps(result, ensure_ascii=False))
        return review.EXITS.get(result["state"], 0)
    except (review.ReviewError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"state": "BLOCKED", "reason": str(exc)}, ensure_ascii=False))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
