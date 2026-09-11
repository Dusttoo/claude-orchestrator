"""Read-only GitHub evidence for sprint progress, never merge authorization."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


class ProgressError(RuntimeError):
    pass


def command_json(root: Path, host: str, endpoint: str, *, pages=False):
    command = ["gh", "api", "--hostname", host, "--method", "GET", endpoint]
    if pages:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProgressError("GitHub progress lookup could not complete") from exc
    if result.returncode:
        raise ProgressError("GitHub progress lookup failed; no progress credit recorded")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise ProgressError("GitHub returned invalid progress evidence") from exc


def repository(root: Path):
    try:
        result = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProgressError("cannot resolve origin for progress verification") from exc
    remote = result.stdout.strip() if result.returncode == 0 else ""
    if re.fullmatch(r"[^/@:]+@[^/:]+:[^:]+", remote):
        host, path = remote.split("@", 1)[1].split(":", 1)
    else:
        parsed = urlsplit(remote)
        if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
            raise ProgressError("progress verification requires an HTTPS or SSH GitHub origin")
        host, path = parsed.hostname, parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        raise ProgressError("origin is not a supported GitHub repository URL")
    # Resolve renamed repositories through GitHub, then bind immutable repo ID.
    repo = command_json(root, host, f"repos/{path}")
    if not isinstance(repo, dict) or not isinstance(repo.get("id"), int):
        raise ProgressError("GitHub omitted repository identity")
    name = repo.get("full_name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        raise ProgressError("GitHub omitted canonical repository name")
    return host.lower(), name, repo["id"]


def number_from_evidence(evidence: str, host: str, name: str) -> int:
    value = evidence.strip()
    if re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    parsed = urlsplit(value)
    prefix = f"/{name}/pull/"
    if (parsed.scheme != "https" or parsed.netloc.lower() != host
            or not parsed.path.lower().startswith(prefix.lower()) or parsed.query or parsed.fragment):
        raise ProgressError("PR evidence must be a number or this repository's canonical PR URL")
    suffix = parsed.path[len(prefix):]
    if not re.fullmatch(r"[1-9][0-9]*", suffix):
        raise ProgressError("invalid PR number")
    return int(suffix)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def observe(root: Path, ticket: dict, milestone: str, evidence: str) -> dict:
    """Fetch evidence outside the checkpoint lock; caller fences the final write."""
    host, name, repo_id = repository(root)
    number = number_from_evidence(evidence, host, name)
    bound_pr = ticket.get("pr")
    if bound_pr and number_from_evidence(str(bound_pr), host, name) != number:
        raise ProgressError("PR differs from this ticket's bound PR")
    if milestone == "ci_advanced" and not bound_pr:
        raise ProgressError("record verified pr_opened progress before CI progress")
    endpoint = f"repos/{name}/pulls/{number}"
    pr = command_json(root, host, endpoint)
    if not isinstance(pr, dict):
        raise ProgressError("GitHub omitted PR evidence")
    head = pr.get("head") or {}
    sha, branch = head.get("sha"), head.get("ref")
    if (pr.get("number") != number or (pr.get("base", {}).get("repo") or {}).get("id") != repo_id
            or pr.get("state") != "open" or not isinstance(branch, str) or not branch
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", sha)):
        raise ProgressError("PR must be open and bound to the expected repository and head")
    if ticket.get("branch") and ticket["branch"] != branch:
        raise ProgressError("PR branch differs from this ticket's branch")
    if sha not in ticket.get("verified_commits", {}) and not any(p.get("verified") and p.get("milestone") == "implementation_commit"
               and p.get("evidence") == sha for p in ticket.get("progress", [])):
        raise ProgressError("PR head must match a verified implementation commit for this ticket")
    try:
        tree_result = subprocess.run(["git", "rev-parse", "--verify", sha + "^{tree}"],
                                     cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProgressError("cannot verify PR code tree") from exc
    tree = tree_result.stdout.strip()
    if tree_result.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", tree):
        raise ProgressError("PR commit is not available in the repository")
    if ticket.get("verified_commits", {}).get(sha, tree) != tree:
        raise ProgressError("PR tree differs from its verified commit receipt")
    receipt = dict(repository_id=repo_id, pr=number, head=sha, tree=tree,
                   url=f"https://{host}/{name}/pull/{number}", branch=branch)
    if milestone == "pr_opened":
        return dict(receipt=receipt, verified=True, fingerprint=digest([repo_id, number]))

    checks = command_json(root, host, f"repos/{name}/commits/{sha}/check-runs?per_page=100&filter=latest", pages=True)
    statuses = command_json(root, host, f"repos/{name}/commits/{sha}/statuses?per_page=100", pages=True)
    if not isinstance(checks, list) or not isinstance(statuses, list):
        raise ProgressError("GitHub omitted paginated CI evidence")
    observed = {}
    latest_ids = {}
    for page in checks:
        if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
            raise ProgressError("invalid check-run page")
        for check in page["check_runs"]:
            if (not isinstance(check, dict) or check.get("head_sha") != sha
                    or not isinstance(check.get("name"), str) or not check["name"]
                    or not isinstance(check.get("id"), int)
                    or not isinstance(check.get("app"), dict)
                    or not isinstance(check["app"].get("id"), int)):
                raise ProgressError("check run does not identify this PR head")
            identity = f"check:{(check.get('app') or {}).get('id')}:{check['name']}"
            state, conclusion = check.get("status"), check.get("conclusion")
            if state not in {"queued", "waiting", "pending", "requested", "in_progress", "completed"}:
                raise ProgressError("unknown check-run state")
            if state == "completed" and conclusion not in {
                "success", "failure", "neutral", "cancelled", "skipped", "timed_out",
                "action_required", "stale", "startup_failure",
            }:
                raise ProgressError("completed check omitted a known conclusion")
            rank = 3 if state == "completed" and conclusion == "success" else 2 if state == "completed" else 1 if state == "in_progress" else 0
            if check["id"] > latest_ids.get(identity, -1):
                latest_ids[identity], observed[identity] = check["id"], rank
    for page in statuses:
        if not isinstance(page, list):
            raise ProgressError("invalid commit-status page")
        for status in page:
            if (not isinstance(status, dict) or not isinstance(status.get("context"), str)
                    or not status["context"] or not isinstance(status.get("id"), int)):
                raise ProgressError("commit status omitted its identity")
            if status.get("state") not in {"pending", "error", "failure", "success"}:
                raise ProgressError("unknown commit-status state")
            identity = "status:" + status["context"]
            rank = 3 if status["state"] == "success" else 2 if status["state"] in {"failure", "error"} else 0
            if status["id"] > latest_ids.get(identity, -1):
                latest_ids[identity], observed[identity] = status["id"], rank
    # Reject a head movement while checks were fetched. This is progress only;
    # the merge guard must still independently verify its current exact head.
    refreshed = command_json(root, host, endpoint)
    if (not isinstance(refreshed, dict) or refreshed.get("state") != "open"
            or refreshed.get("number") != number
            or (refreshed.get("head") or {}).get("sha") != sha
            or (refreshed.get("head") or {}).get("ref") != branch
            or (refreshed.get("base", {}).get("repo") or {}).get("id") != repo_id):
        raise ProgressError("PR changed during CI verification; retry against its current head")
    previous = (ticket.get("ci_progress") or {}).get(tree, {})
    advanced = {key: rank for key, rank in observed.items() if rank > previous.get(key, 0)}
    highest = {**previous}
    for key, rank in observed.items():
        highest[key] = max(rank, highest.get(key, 0))
    receipt["checks"] = observed
    receipt["advanced"] = advanced
    return dict(receipt=receipt, verified=bool(advanced),
                fingerprint=digest([repo_id, number, tree, highest]), ci_highest=highest)
