"""Controller-executed test observations; progress evidence, never a merge gate."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import xml.etree.ElementTree as ET


class TestProgressError(RuntimeError):
    pass


def cases_from_report(report, returncode):
    try:
        root = ET.fromstring(report)
    except ET.ParseError as exc:
        raise TestProgressError("test runner produced invalid JUnit XML") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise TestProgressError("test runner must produce JUnit suites")
    try:
        suite_errors = any(int(suite.get("errors", "0")) != 0
                           for suite in root.iter("testsuite"))
    except ValueError as exc:
        raise TestProgressError("invalid JUnit error count") from exc
    if suite_errors or any(True for _ in root.iter("error")):
        raise TestProgressError("runner errors grant no progress")
    cases = {}
    for case in root.iter("testcase"):
        identity = json.dumps([case.get("classname", ""), case.get("name", "")])
        if not case.get("name") or identity in cases:
            raise TestProgressError("JUnit cases require unique class/name identities")
        cases[identity] = ("error" if case.find("error") is not None else
                           "skipped" if case.find("skipped") is not None else
                           "failed" if case.find("failure") is not None else "passed")
    if (not cases or "error" in cases.values() or returncode not in {0, 1}
            or (returncode == 0) != ("failed" not in cases.values())):
        raise TestProgressError("empty, errored, or inconsistent test run grants no progress")
    return cases


def observe(root, ticket, config, milestone, evidence):
    try:
        request = json.loads(evidence)
        name, commit = request["check"], request["commit"]
        definition = config["progress_tests"][name]
        command = definition["command"]
        timeout = definition.get("timeout_seconds", 120)
        if (not isinstance(command, list) or not command or
                not all(isinstance(arg, str) and arg for arg in command) or
                not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 600 or
                not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit)):
            raise ValueError()
    except (ValueError, TypeError, KeyError) as exc:
        raise TestProgressError('test evidence requires a configured "check" and full "commit" SHA') from exc

    def git(*args):
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
        if result.returncode:
            raise TestProgressError("test snapshot Git validation failed")
        return result.stdout.strip()

    baseline = (ticket.get("launch_evidence") or {}).get("base_commit")
    if not baseline:
        raise TestProgressError("test progress requires a launch baseline")
    git("merge-base", "--is-ancestor", baseline, commit)
    tree = git("rev-parse", commit + "^{tree}")
    definition_id = hashlib.sha256(json.dumps([name, command], sort_keys=True).encode()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="orka-test-progress-") as directory:
        checkout = Path(directory) / "checkout"
        report = Path(directory) / "report.xml"
        git("worktree", "add", "--detach", str(checkout), commit)
        try:
            with (Path(directory) / "output.log").open("wb") as output:
                process = subprocess.Popen(command, cwd=checkout,
                    env={**os.environ, "ORKA_TEST_REPORT": str(report)},
                    stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
                try:
                    returncode = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    raise TestProgressError("test command timed out; no progress credited") from exc
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
            if git("-C", str(checkout), "rev-parse", "HEAD") != commit or git(
                    "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"):
                raise TestProgressError("test command changed tracked snapshot files")
            if report.is_symlink() or not report.is_file() or report.stat().st_size > 8 * 1024 * 1024:
                raise TestProgressError("test runner must write a bounded fresh JUnit report to ORKA_TEST_REPORT")
            with report.open("rb") as source:
                raw = source.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise TestProgressError("JUnit report exceeds size limit")
            cases = cases_from_report(raw, returncode)
        except OSError as exc:
            raise TestProgressError("test command could not execute or produce a report") from exc
        finally:
            git("worktree", "remove", "--force", str(checkout))
    previous = ticket.get("test_progress", {}).get(definition_id, {})
    updated = dict(previous)
    advanced = []
    for identity, status in cases.items():
        old = previous.get(identity, {})
        if milestone == "failing_test" and status == "failed" and not old:
            updated[identity] = {"stage": "failed", "commit": commit}
            advanced.append(identity)
        elif milestone == "tests_repaired" and status == "passed" and old.get("stage") == "failed":
            if commit == old["commit"]:
                continue
            git("merge-base", "--is-ancestor", old["commit"], commit)
            if tree == git("rev-parse", old["commit"] + "^{tree}"):
                continue
            updated[identity] = {"stage": "repaired", "commit": commit}
            advanced.append(identity)
    receipt = dict(check=name, definition=definition_id, commit=commit, tree=tree,
                   report_sha256=hashlib.sha256(raw).hexdigest(), returncode=returncode,
                   cases=cases, advanced=sorted(advanced))
    fingerprint = hashlib.sha256(json.dumps([definition_id, milestone, sorted(advanced)]).encode()).hexdigest()
    return dict(verified=bool(advanced), fingerprint=fingerprint, receipt=receipt,
                definition=definition_id, cases=updated)
