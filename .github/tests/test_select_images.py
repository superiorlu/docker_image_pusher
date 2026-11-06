"""Run: python3 -m unittest discover -s .github/tests -v.

Registry queries are the external boundary; selection and naming use real code.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'select_missing_images.py'
REGISTRY = 'acr.example.invalid'
NAMESPACE = 'hub518'


class RegistrySelectionTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'ACR existence-based selection is not implemented yet')
        spec = importlib.util.spec_from_file_location('select_missing_images', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_checks_unchanged_entries_and_selects_only_missing_targets(self):
        checked = []
        def inspect(target):
            checked.append(target)
            return target.endswith('/already:v1')
        selected, skipped = self.module.plan_sync(
            ['team/already:v1', 'team/failed-last-time:v1'], REGISTRY, NAMESPACE,
            inspect=inspect)
        self.assertEqual(checked, [REGISTRY + '/hub518/already:v1',
                                   REGISTRY + '/hub518/failed-last-time:v1'])
        self.assertEqual(selected, [('team/failed-last-time:v1', REGISTRY + '/hub518/failed-last-time:v1')])
        self.assertEqual(skipped, [('team/already:v1', REGISTRY + '/hub518/already:v1')])

    def test_failed_image_is_selected_next_run_then_skipped_after_success(self):
        entries = ['team/retry:v1']
        selected, _ = self.module.plan_sync(entries, REGISTRY, NAMESPACE, inspect=lambda _: False)
        self.assertEqual(selected, [('team/retry:v1', REGISTRY + '/hub518/retry:v1')])
        selected, _ = self.module.plan_sync(entries, REGISTRY, NAMESPACE, inspect=lambda _: False)
        self.assertEqual(selected, [('team/retry:v1', REGISTRY + '/hub518/retry:v1')])
        selected, skipped = self.module.plan_sync(entries, REGISTRY, NAMESPACE, inspect=lambda _: True)
        self.assertEqual(selected, [])
        self.assertEqual(len(skipped), 1)

    def test_manual_filter_uses_full_list_for_duplicate_destination_names(self):
        checked = []
        selected, _ = self.module.plan_sync(
            ['alice/app:v1', 'bob/app:v1'], REGISTRY, NAMESPACE,
            requested='bob/app:v1', inspect=lambda ref: checked.append(ref) or False)
        self.assertEqual(checked, [REGISTRY + '/hub518/bob_app:v1'])
        self.assertEqual(selected, [('bob/app:v1', REGISTRY + '/hub518/bob_app:v1')])

    def test_comments_whitespace_and_duplicates_do_not_repeat_queries(self):
        entries = self.module.entries(' # comment\n\n team/app:v1  \nteam/app:v1\n')
        checked = []
        selected, _ = self.module.plan_sync(entries, REGISTRY, NAMESPACE,
                                            inspect=lambda ref: checked.append(ref) or False)
        self.assertEqual(checked, [REGISTRY + '/hub518/app:v1'])
        self.assertEqual(selected, [('team/app:v1', REGISTRY + '/hub518/app:v1')])

    def test_explicit_force_all_preserves_manual_resync_capability(self):
        selected, skipped = self.module.plan_sync(
            ['team/app:v1'], REGISTRY, NAMESPACE, force_all=True,
            inspect=lambda _: self.fail('Force must not depend on registry existence'))
        self.assertEqual(selected, [('team/app:v1', REGISTRY + '/hub518/app:v1')])
        self.assertEqual(skipped, [])

    def test_unknown_or_conflicting_manual_request_fails_before_queries(self):
        for requested, force in [('unknown:v1', False), ('team/app:v1', True)]:
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                self.module.plan_sync(['team/app:v1'], REGISTRY, NAMESPACE,
                    requested=requested, force_all=force,
                    inspect=lambda _: self.fail('Invalid input must not query'))

    def test_destination_mapping_preserves_current_names_and_platform_options(self):
        fixtures = [
            ('openwebui/open-webui:0.12.5', 'open-webui:0.12.5'),
            ('gitlab/gitlab-runner:alpine-v19.5.0', 'gitlab-runner:alpine-v19.5.0'),
            ('ghcr.io/cirruslabs/flutter:3.41.7', 'flutter:3.41.7'),
            ('--platform linux/amd64 team/app:v1', 'linux_amd64_app:v1'),
            ('--platform=linux/arm64 team/app:v1', 'linux_arm64_app:v1'),
            ('team/app:v1@sha256:abc', 'app:v1'),
        ]
        for entry, name in fixtures:
            with self.subTest(entry=entry):
                selected, _ = self.module.plan_sync([entry], REGISTRY, NAMESPACE, inspect=lambda _: False)
                self.assertEqual(selected, [(entry, REGISTRY + '/hub518/' + name)])

    def test_versions_from_same_namespace_do_not_gain_namespace_prefix(self):
        selected, _ = self.module.plan_sync(['team/app:v1', 'team/app:v2'], REGISTRY,
                                            NAMESPACE, inspect=lambda _: False)
        self.assertEqual(selected, [('team/app:v1', REGISTRY + '/hub518/app:v1'),
                                    ('team/app:v2', REGISTRY + '/hub518/app:v2')])

    def test_existing_manifest_is_metadata_only_check(self):
        target = REGISTRY + '/hub518/app:v1'
        for manifest in [{'schemaVersion': 2, 'config': {'digest': 'sha256:abc'}, 'layers': []},
                         {'schemaVersion': 2, 'manifests': []}]:
            with self.subTest(manifest=manifest), mock.patch.object(self.module.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(manifest), '')) as run:
                self.assertTrue(self.module.manifest_exists(target))
                run.assert_called_once_with(['docker', 'buildx', 'imagetools', 'inspect', target, '--raw'],
                                            capture_output=True, text=True, timeout=60)

    def test_only_explicit_manifest_not_found_selects_for_sync(self):
        target = REGISTRY + '/hub518/app:v1'
        for error in ['ERROR: ' + target + ': not found', 'manifest unknown',
                      'MANIFEST_UNKNOWN: manifest unknown', 'no such manifest: ' + target,
                      'NAME_UNKNOWN: repository name not known']:
            with self.subTest(error=error), mock.patch.object(self.module.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 1, '', error)):
                self.assertFalse(self.module.manifest_exists(target))

    def test_not_found_is_not_confused_by_words_in_the_image_reference(self):
        target = REGISTRY + '/hub518/denied:500'
        with mock.patch.object(self.module.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 1, '', 'ERROR: ' + target + ': not found')):
            self.assertFalse(self.module.manifest_exists(target))

    def test_auth_network_server_and_ambiguous_errors_do_not_mean_missing(self):
        target = REGISTRY + '/hub518/app:v1'
        errors = ['401 Unauthorized', '403 Forbidden', 'insufficient_scope: authorization failed',
                  'pull access denied, repository does not exist or may require authorization',
                  '500 Internal Server Error', '502 Bad Gateway', '429 Too Many Requests',
                  'lookup host: no such host', 'TLS handshake timeout',
                  'certificate file not found', 'unexpected status from token endpoint: 404 Not Found',
                  'unauthorized: manifest unknown']
        for error in errors:
            with self.subTest(error=error), mock.patch.object(self.module.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 1, '', error)):
                with self.assertRaises(RuntimeError):
                    self.module.manifest_exists(target)

    def test_timeout_and_invalid_success_response_fail_closed(self):
        with mock.patch.object(self.module.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired(['docker'], 60)):
            with self.assertRaises(RuntimeError):
                self.module.manifest_exists(REGISTRY + '/hub518/app:v1')
        for content in ['not json', '{}']:
            with self.subTest(content=content), mock.patch.object(self.module.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, content, '')):
                with self.assertRaises(RuntimeError):
                    self.module.manifest_exists(REGISTRY + '/hub518/app:v1')

    def test_errors_redact_password_if_cli_includes_it(self):
        with mock.patch.dict(os.environ, {'ALIYUN_REGISTRY_PASSWORD': 'dummy-secret'}), \
             mock.patch.object(self.module.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 1, '', 'error dummy-secret')):
            with self.assertRaises(RuntimeError) as error:
                self.module.manifest_exists(REGISTRY + '/hub518/app:v1')
        self.assertNotIn('dummy-secret', str(error.exception))

    def test_main_writes_source_destination_pairs_and_skip_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'images.txt').write_text('team/exists:v1\nteam/missing:v1\n')
            env = {'ALIYUN_REGISTRY': REGISTRY, 'ALIYUN_NAME_SPACE': NAMESPACE,
                   'INPUT_IMAGES': '', 'INPUT_SYNC_ALL': 'false',
                   'SELECTED_IMAGES_FILE': str(root / 'selected'), 'GITHUB_OUTPUT': str(root / 'output')}
            with mock.patch.dict(os.environ, env), mock.patch.object(self.module, 'manifest_exists',
                    side_effect=lambda ref: ref.endswith('/exists:v1')), contextlib.redirect_stdout(io.StringIO()):
                self.module.main(root / 'images.txt')
            self.assertEqual((root / 'selected').read_text(),
                             'team/missing:v1\tacr.example.invalid/hub518/missing:v1\n')
            self.assertEqual((root / 'output').read_text(), 'count=1\nskipped=1\n')

    def test_registry_error_never_publishes_partial_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'images.txt').write_text('team/missing:v1\nteam/error:v1\n')
            env = {'ALIYUN_REGISTRY': REGISTRY, 'ALIYUN_NAME_SPACE': NAMESPACE,
                   'INPUT_IMAGES': '', 'INPUT_SYNC_ALL': 'false',
                   'SELECTED_IMAGES_FILE': str(root / 'selected'), 'GITHUB_OUTPUT': str(root / 'output')}
            with mock.patch.dict(os.environ, env), mock.patch.object(self.module, 'manifest_exists',
                    side_effect=[False, RuntimeError('permission denied')]):
                with self.assertRaises(RuntimeError):
                    self.module.main(root / 'images.txt')
            self.assertFalse((root / 'selected').exists())
            self.assertFalse((root / 'output').exists())


if __name__ == '__main__':
    unittest.main()
