"""Allow reviewed in-tree crate replacements without changing external pins."""
import argparse
import json
from pathlib import Path
import sys
import tomllib


IN_TREE = frozenset({
    'platform-api', 'platform-provider', 'platform-pyo3',
    'platform-native', 'platform-base', 'swss-common',
})


def identity(package):
    return tuple(package.get(key) for key in ('name', 'version', 'source', 'checksum'))


def component_packages(config):
    """Read locks beside the explicitly selected in-tree patch packages."""
    if config is None:
        return []
    path = Path(config).resolve()
    data = tomllib.loads(path.read_text())
    locks = set()
    for entries in data.get('patch', {}).values():
        for entry in entries.values():
            if not isinstance(entry, dict) or 'path' not in entry:
                continue
            package = (path.parent.parent / entry['path']).resolve()
            for directory in (package, package.parent):
                lock = directory / 'Cargo.lock'
                if lock.is_file():
                    locks.add(lock)
    return [package for lock in sorted(locks)
            for package in tomllib.loads(lock.read_text())['package']]


def problems(pinned, resolved, components=()):
    previous = {identity(package) for package in pinned['package']}
    names = {package['name'] for package in pinned['package']}
    additions = {identity(package) for package in components if 'source' in package}
    local = {identity(package) for package in pinned['package'] if 'source' not in package}
    errors = []
    for package in resolved['package']:
        key = identity(package)
        if 'source' in package:
            if key not in previous and not (package['name'] not in names and key in additions):
                errors.append('external dependency differs from pinned Cargo.lock: '
                              f"{package['name']} {package['version']}")
        elif key not in local and package['name'] not in IN_TREE:
            errors.append('unexpected local dependency: ' + package['name'])
    return errors


def prepare(pinned_text, components):
    """Seed native-only dependencies from reviewed component locks before resolution."""
    pinned = tomllib.loads(pinned_text)
    names = {package['name'] for package in pinned['package']}
    selected = {}
    for package in components:
        if 'source' not in package or package['name'] in names:
            continue
        selected[identity(package)] = package
    result = pinned_text.rstrip() + '\n'
    for key in sorted(selected):
        result += '\n[[package]]\n'
        for field, value in selected[key].items():
            result += field + ' = ' + json.dumps(value) + '\n'
    return result


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--config')
    parser.add_argument('pinned', type=Path)
    parser.add_argument('resolved', type=Path)
    args = parser.parse_args(argv)
    components = component_packages(args.config)
    pinned_text = args.pinned.read_text()
    if args.prepare:
        args.resolved.write_text(prepare(pinned_text, components))
        return 0
    pinned = tomllib.loads(pinned_text)
    resolved = tomllib.loads(args.resolved.read_text())
    errors = problems(pinned, resolved, components)
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print('Patched Cargo.lock preserves external pins and reviewed native additions')
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
