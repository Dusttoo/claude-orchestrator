"""Real committed test runs and fenced controller progress receipts."""
import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import test_progress as progress
SPEC = importlib.util.spec_from_file_location("test_progress_controller", ROOT / "scripts/sprint-controller.py")
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)
RUNNER = '''import os, unittest, xml.etree.ElementTree as ET
from pathlib import Path
class Behavior(unittest.TestCase):
    def test_fixed(self):
        self.assertEqual(Path('value').read_text(), 'fixed')
result = unittest.TestResult()
unittest.defaultTestLoader.loadTestsFromTestCase(Behavior).run(result)
suite = ET.Element('testsuite')
case = ET.SubElement(suite, 'testcase', classname='Behavior', name='test_fixed')
if result.failures: ET.SubElement(case, 'failure').text = result.failures[0][1]
if result.errors: ET.SubElement(case, 'error').text = result.errors[0][1]
ET.ElementTree(suite).write(os.environ['ORKA_TEST_REPORT'])
raise SystemExit(0 if result.wasSuccessful() else 1)
'''


class TestProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        (self.root / 'runner.py').write_text(RUNNER)
        self.base = self.commit('broken')
        self.ticket = dict(key='T-1', state='running', attempt_token='cap', history=[], progress=[],
                           launch_evidence=dict(base_commit=self.base))
        self.config = dict(progress_tests=dict(unit=dict(command=[sys.executable, 'runner.py'])))

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, value):
        (self.root / 'value').write_text(value)
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', value)
        return self.git('rev-parse', 'HEAD')

    def observe(self, milestone, commit=None):
        observation = progress.observe(self.root, self.ticket, self.config, milestone,
            json.dumps(dict(check='unit', commit=commit or self.base)))
        self.ticket.setdefault('test_progress', {})[observation['definition']] = observation['cases']
        return observation

    def test_failure_then_repair_credits_each_transition_once(self):
        self.assertTrue(self.observe('failing_test')['verified'])
        self.assertFalse(self.observe('failing_test')['verified'])
        fixed = self.commit('fixed')
        self.assertTrue(self.observe('tests_repaired', fixed)['verified'])
        self.assertFalse(self.observe('tests_repaired', fixed)['verified'])
        broken = self.commit('broken again')
        self.assertFalse(self.observe('failing_test', broken)['verified'])
        self.assertEqual(len(self.git('worktree', 'list', '--porcelain').split('worktree ')), 2)

    def test_unobserved_failure_cannot_be_claimed_repaired(self):
        self.assertFalse(self.observe('tests_repaired', self.commit('fixed'))['verified'])

    def test_workspace_changes_do_not_affect_committed_snapshot(self):
        (self.root / 'value').write_text('fixed')
        self.assertTrue(self.observe('failing_test')['verified'])
        self.assertEqual((self.root / 'value').read_text(), 'fixed')

    def test_invalid_reports_and_runner_failures_get_no_credit(self):
        for report, code in [(b'<testsuite/>', 0), (b'bad', 1),
            (b'<testsuite><testcase name="a"><error/></testcase></testsuite>', 1),
            (b'<testsuite errors="1"><testcase name="a"/></testsuite>', 0),
            (b'<testsuite><testcase name="a"/></testsuite>', 1),
            (b'<testsuite><testcase name="a"><failure/></testcase></testsuite>', 0),
            (b'<testsuite><testcase name="a"/><testcase name="a"/></testsuite>', 0)]:
            with self.subTest(report=report), self.assertRaises(progress.TestProgressError):
                progress.cases_from_report(report, code)

    def test_timeout_and_missing_report_cleanup_snapshot(self):
        for command in ([sys.executable, '-c', 'import time; time.sleep(10)'], [sys.executable, '-c', 'pass']):
            self.config['progress_tests']['unit'] = dict(command=command, timeout_seconds=1)
            with self.assertRaises(progress.TestProgressError):
                self.observe('failing_test')
            self.assertEqual(len(self.git('worktree', 'list', '--porcelain').split('worktree ')), 2)

    def test_tracked_snapshot_mutation_cannot_earn_progress(self):
        self.config['progress_tests']['unit']['command'] = [sys.executable, '-c',
            "from pathlib import Path; Path('value').write_text('changed')"]
        with self.assertRaisesRegex(progress.TestProgressError, 'changed tracked'):
            self.observe('failing_test')

    def test_skipped_or_removed_cases_do_not_claim_repair(self):
        self.observe('failing_test')
        (self.root / 'runner.py').write_text(RUNNER.replace(
            "if result.failures: ET.SubElement(case, 'failure').text = result.failures[0][1]",
            "ET.SubElement(case, 'skipped')").replace(
            'raise SystemExit(0 if result.wasSuccessful() else 1)', 'raise SystemExit(0)'))
        changed = self.commit('still broken')
        self.assertFalse(self.observe('tests_repaired', changed)['verified'])

    def checkpoint(self):
        config = self.root / 'config.json'
        config.write_text('progress_tests:\n  unit:\n    command: ' + json.dumps(self.config['progress_tests']['unit']['command']) + '\n')
        cfg = dict(shared_root=self.root, state_dir=self.root / 'state', config=config)
        path = controller.state_path(cfg['state_dir'], '1')
        path.parent.mkdir(parents=True)
        controller.save(path, dict(schema_version=2, sprint=dict(id='1'), tickets={'T-1': self.ticket}))
        args = argparse.Namespace(sprint='1', ticket='T-1', attempt_token='cap', milestone='failing_test',
                                  evidence=json.dumps(dict(check='unit', commit=self.base)))
        return cfg, path, args

    def test_controller_replay_does_not_reset_watchdog(self):
        cfg, path, args = self.checkpoint()
        with patch.object(controller, 'usage_snapshots', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            controller.record_progress(args, cfg)
            controller.record_progress(args, cfg)
        events = controller.load(path)['tickets']['T-1']['progress']
        self.assertEqual(sum(e['verified'] for e in events), 1)
        self.assertIn('report_sha256', events[0]['receipt'])

    def test_attempt_change_during_execution_discards_receipt(self):
        cfg, path, args = self.checkpoint()
        original = progress.observe
        def changed(*values):
            result = original(*values)
            with controller.locked(path):
                state = controller.load(path)
                state['tickets']['T-1']['attempt_token'] = 'replacement'
                controller.save(path, state)
            return result
        with patch.object(progress, 'observe', side_effect=changed), self.assertRaises(controller.SprintError):
            controller.record_progress(args, cfg)
        self.assertFalse(controller.load(path)['tickets']['T-1']['progress'])


if __name__ == '__main__':
    unittest.main()
