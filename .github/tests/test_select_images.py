"""Run with: python3 -m unittest discover -s .github/tests -v.

Exercise the workflow's actual selection code against real git histories.
Catches accidental full sync, lost additions and unsafe manual defaults.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / 'workflows' / 'docker.yaml'


class SelectImagesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, text=True).strip()

    def commit(self, content):
        (self.repo / 'images.txt').write_text(content)
        self.git('add', 'images.txt')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def select(self, event='push', before='', images='', full='false'):
        source = WORKFLOW.read_text()
        start = '        # BEGIN IMAGE SELECTION\n'
        end = '        # END IMAGE SELECTION\n'
        self.assertTrue(start in source, 'Workflow has no incremental image selection yet')
        script = textwrap.dedent(source.split(start, 1)[1].split(end, 1)[0])
        output = self.repo / 'output'
        selected = self.repo / 'selected'
        env = dict(os.environ, EVENT_NAME=event, BEFORE_SHA=before,
                   INPUT_IMAGES=images, INPUT_SYNC_ALL=full,
                   GITHUB_OUTPUT=str(output), SELECTED_IMAGES_FILE=str(selected))
        result = subprocess.run(['python3', '-c', script], cwd=self.repo,
                                env=env, text=True, capture_output=True)
        lines = selected.read_text().splitlines() if selected.exists() else []
        return result, lines, output.read_text() if output.exists() else ''

    def test_push_selects_only_new_image(self):
        before = self.commit('old:v1\n')
        self.commit('old:v1\nnew:v2\n')
        result, lines, output = self.select(before=before)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines, ['new:v2'])
        self.assertEqual(output, 'count=1\n')

    def test_comments_reordering_duplicates_and_removals_do_not_pull(self):
        before = self.commit('old:v1\nsecond:v1\nremoved:v1\n')
        self.commit(' # comment\nsecond:v1\n\n  old:v1  \nold:v1\n')
        result, lines, output = self.select(before=before)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines, [])
        self.assertEqual(output, 'count=0\n')

    def test_compares_entire_multi_commit_push(self):
        before = self.commit('old:v1\n')
        self.commit('old:v1\nnew:v2\n')
        self.commit('old:v1\nnew:v2\nnew:v3\n')
        result, lines, _ = self.select(before=before)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines, ['new:v2', 'new:v3'])

    def test_platform_options_and_whitespace(self):
        before = self.commit('--platform linux/amd64 old:v1\n')
        self.commit('  --platform   linux/amd64  old:v1\n--platform=linux/arm64 new:v2\n')
        result, lines, _ = self.select(before=before)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines, ['--platform=linux/arm64 new:v2'])

    def test_manual_retry_selects_only_requested_list_entry(self):
        self.commit('old:v1\nnew:v2\n')
        result, lines, _ = self.select(event='workflow_dispatch', images='new:v2\nnew:v2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines, ['new:v2'])

    def test_manual_full_sync_is_explicit(self):
        self.commit('old:v1\nnew:v2\n')
        result, lines, _ = self.select(event='workflow_dispatch', full='true')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines, ['old:v1', 'new:v2'])

    def test_manual_empty_unknown_or_conflicting_input_fails_closed(self):
        self.commit('old:v1\n')
        for images, full in [('', 'false'), ('unknown:v1', 'false'), ('old:v1', 'true')]:
            with self.subTest(images=images, full=full):
                result, lines, _ = self.select(event='workflow_dispatch', images=images, full=full)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(lines, [])

    def test_missing_base_fails_without_full_sync(self):
        self.commit('old:v1\n')
        result, lines, _ = self.select(before='1' * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(lines, [])

    def test_new_branch_selects_all_as_new(self):
        self.commit('old:v1\nnew:v2\n')
        result, lines, _ = self.select(before='0' * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines, ['old:v1', 'new:v2'])


if __name__ == '__main__':
    unittest.main()
