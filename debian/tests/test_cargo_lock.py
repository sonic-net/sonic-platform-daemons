"""The in-tree build may replace crates, but may not update external pins."""
import copy
from pathlib import Path
import runpy
import tomllib
import unittest

CHECK = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'verify-cargo-lock.py'))
PROBLEMS = CHECK['problems']


class CargoLockTests(unittest.TestCase):
    def setUp(self):
        self.pinned = {'package': [
            {'name': 'sonic-thermalctld-rs', 'version': '1.0.0'},
            {'name': 'platform-api', 'version': '0.1.0', 'source': 'git+https://example.test/common#abc'},
            {'name': 'pyo3', 'version': '0.30.1', 'source': 'registry+https://example.test/index',
             'checksum': 'reviewed-checksum'},
        ]}

    def test_in_tree_provider_and_its_local_dependencies_are_allowed(self):
        resolved = copy.deepcopy(self.pinned)
        resolved['package'][1].pop('source')
        resolved['package'].append({'name': 'platform-base', 'version': '0.1.0'})
        self.assertEqual(PROBLEMS(self.pinned, resolved), [])

    def test_external_version_checksum_or_source_change_is_rejected(self):
        for field, value in [('version', '0.31.0'), ('checksum', 'different'),
                             ('source', 'registry+https://different.test/index')]:
            with self.subTest(field=field):
                resolved = copy.deepcopy(self.pinned)
                resolved['package'][2][field] = value
                self.assertTrue(PROBLEMS(self.pinned, resolved))

    def test_unreviewed_external_and_local_packages_are_rejected(self):
        for package in [
            {'name': 'new-dependency', 'version': '1.0', 'source': 'git+https://example.test/new#def'},
            {'name': 'new-local-dependency', 'version': '1.0'},
        ]:
            with self.subTest(package=package):
                resolved = copy.deepcopy(self.pinned)
                resolved['package'].append(package)
                self.assertTrue(PROBLEMS(self.pinned, resolved))

    def test_unused_packages_may_be_removed_by_provider_selection(self):
        resolved = {'package': [self.pinned['package'][0]]}
        self.assertEqual(PROBLEMS(self.pinned, resolved), [])

    def test_native_only_external_package_must_match_a_component_lock(self):
        package = {'name': 'indexmap', 'version': '2.0.0',
                   'source': 'registry+https://example.test/index', 'checksum': 'reviewed'}
        resolved = copy.deepcopy(self.pinned)
        resolved['package'].append(package)
        self.assertTrue(PROBLEMS(self.pinned, resolved))
        self.assertEqual(PROBLEMS(self.pinned, resolved, [package]), [])

    def test_component_lock_cannot_override_an_existing_external_pin(self):
        resolved = copy.deepcopy(self.pinned)
        resolved['package'][2]['version'] = '0.31.0'
        self.assertTrue(PROBLEMS(self.pinned, resolved, resolved['package']))

    def test_prepare_seeds_only_new_external_component_packages(self):
        original = 'version = 4\n[[package]]\nname = "pyo3"\nversion = "0.30.1"\n'
        components = [
            {'name': 'pyo3', 'version': '0.31.0', 'source': 'registry+https://example.test/index'},
            {'name': 'platform-base', 'version': '0.1.0'},
            {'name': 'indexmap', 'version': '2.0.0', 'source': 'registry+https://example.test/index'},
        ]
        prepared = tomllib.loads(CHECK['prepare'](original, components))
        self.assertEqual([(p['name'], p['version']) for p in prepared['package']],
                         [('pyo3', '0.30.1'), ('indexmap', '2.0.0')])


if __name__ == '__main__':
    unittest.main()
