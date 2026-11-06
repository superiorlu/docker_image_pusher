"""Compare platform manifests and copy changed images without changing digests.

--compare-digests examines the whole list; --sync-plan copies pinned sources.
Docker and Skopeo reuse the workflow's ACR login. The no-argument existence
planner remains compatible with the preceding workflow during the rollout.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def entries(text):
    return list(dict.fromkeys(
        ' '.join(line.split()) for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    ))


def image_parts(entry):
    parts = entry.split()
    image = parts[-1]
    platform = ''
    for index, part in enumerate(parts[:-1]):
        if part.startswith('--platform='):
            platform = part.split('=', 1)[1]
            break
        if part == '--platform':
            platform = parts[index + 1]
            break
    paths = image.split('@', 1)[0].split('/')
    namespace = paths[1] if len(paths) == 3 else paths[0] if len(paths) == 2 else ''
    name_tag = paths[-1]
    return image, platform, namespace, name_tag


def manifest_exists(reference):
    try:
        result = subprocess.run(
            ['docker', 'buildx', 'imagetools', 'inspect', reference, '--raw'],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError('ACR manifest check timed out: ' + reference) from None
    if result.returncode == 0:
        try:
            manifest = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise RuntimeError('Invalid ACR manifest response: ' + reference) from None
        if not isinstance(manifest, dict) or manifest.get('schemaVersion') != 2 or not (
            isinstance(manifest.get('config'), dict) or isinstance(manifest.get('manifests'), list)
        ):
            raise RuntimeError('Invalid ACR manifest response: ' + reference)
        return True

    error = result.stderr.strip()
    # Never interpret ambiguous "does not exist or may require authorization"
    # errors, failed token requests, or transport failures as a missing image.
    diagnostic = error.replace(reference, '')
    ambiguous = re.search(
        r'unauthoriz|forbidden|denied|insufficient_scope|authentication|authorization|'
        r'timeout|timed out|certificate|connection|no such host|TLS|'
        r'\b(?:401|403|429|5\d\d)\b', diagnostic, re.IGNORECASE,
    )
    missing = re.search(
        r'\b(?:manifest unknown|manifest_unknown|name_unknown|no such manifest)\b',
        error, re.IGNORECASE,
    ) or error.casefold().endswith(reference.casefold() + ': not found')
    if missing and not ambiguous:
        return False
    password = os.environ.get('ALIYUN_REGISTRY_PASSWORD', '')
    if password:
        error = error.replace(password, '***')
    raise RuntimeError('Cannot determine ACR tag existence for ' + reference + ': ' + error)


def destination_pairs(all_entries, registry, namespace, requested='', force_all=False):
    requested_entries = entries(requested)
    if force_all and requested_entries:
        raise ValueError('Choose specific images OR force sync_all, not both.')
    unknown = [entry for entry in requested_entries if entry not in all_entries]
    if unknown:
        raise ValueError('Requested entry is not in images.txt: ' + ', '.join(unknown))
    candidates = requested_entries or all_entries

    # Preserve the original destination naming, based on the COMPLETE list.
    names = {}
    for entry in all_entries:
        _, _, source_namespace, name_tag = image_parts(entry)
        names.setdefault(name_tag.split(':', 1)[0], set()).add(source_namespace)
    duplicates = {name for name, namespaces in names.items() if len(namespaces) > 1}

    pairs = []
    for entry in candidates:
        _, platform, source_namespace, name_tag = image_parts(entry)
        platform_prefix = platform.replace('/', '_') + '_' if platform else ''
        namespace_prefix = source_namespace + '_' if (
            name_tag.split(':', 1)[0] in duplicates and source_namespace
        ) else ''
        destination = (registry.rstrip('/') + '/' + namespace.strip('/') + '/' +
                       platform_prefix + namespace_prefix + name_tag)
        pairs.append((entry, destination))
    return pairs


def plan_sync(all_entries, registry, namespace, requested='', force_all=False, inspect=None):
    inspect = inspect or manifest_exists
    selected, skipped = [], []
    for pair in destination_pairs(all_entries, registry, namespace, requested, force_all):
        entry, destination = pair
        if not force_all and inspect(destination):
            skipped.append(pair)
        else:
            selected.append(pair)
    return selected, skipped


def registry_query(reference, *options, missing_ok=False):
    """Read metadata with the Docker login credentials; never fetch image layers."""
    try:
        result = subprocess.run(
            ['docker', 'buildx', 'imagetools', 'inspect', reference, *options],
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError('Registry metadata check timed out: ' + reference) from None
    if result.returncode == 0:
        return result.stdout
    error = result.stderr.decode('utf-8', errors='replace').strip()
    diagnostic = error.replace(reference, '')
    ambiguous = re.search(
        r'unauthoriz|forbidden|denied|insufficient_scope|authentication|authorization|'
        r'timeout|timed out|certificate|connection|no such host|TLS|'
        r'\b(?:401|403|429|5\d\d)\b', diagnostic, re.IGNORECASE,
    )
    missing = re.search(
        r'\b(?:manifest unknown|manifest_unknown|name_unknown|no such manifest)\b',
        error, re.IGNORECASE,
    ) or error.casefold().endswith(reference.casefold() + ': not found')
    if missing_ok and missing and not ambiguous:
        return None
    password = os.environ.get('ALIYUN_REGISTRY_PASSWORD', '')
    if password:
        error = error.replace(password, '***')
    raise RuntimeError('Cannot read registry metadata for ' + reference + ': ' + error)


def read_manifest(reference, missing_ok=False):
    raw = registry_query(reference, '--raw', missing_ok=missing_ok)
    if raw is None:
        return None
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError('Invalid registry manifest: ' + reference) from None
    if not isinstance(manifest, dict) or manifest.get('schemaVersion') != 2 or not (
        isinstance(manifest.get('manifests'), list) or
        isinstance(manifest.get('config'), dict) and isinstance(manifest.get('layers'), list)
    ):
        raise RuntimeError('Invalid registry manifest: ' + reference)
    return {'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'manifest': manifest}


def pin_reference(reference, digest):
    repository = reference.split('@', 1)[0]
    # A registry port is not a tag separator.
    if repository.rfind(':') > repository.rfind('/'):
        repository = repository.rsplit(':', 1)[0]
    return repository + '@' + digest


def resolve_manifest(reference, platform, missing_ok=False, verify_platform=False):
    wanted = platform.split('/')
    if len(wanted) not in (2, 3) or not all(wanted):
        raise ValueError('Expected os/architecture[/variant], got: ' + platform)
    found = read_manifest(reference, missing_ok=missing_ok)
    if found is None:
        return None
    if 'manifests' in found['manifest']:
        candidates = []
        for descriptor in found['manifest']['manifests']:
            actual = descriptor.get('platform', {})
            if actual.get('os') == wanted[0] and actual.get('architecture') == wanted[1] and (
                len(wanted) == 2 or actual.get('variant') == wanted[2]
            ):
                candidates.append(descriptor)
        if not candidates and missing_ok:
            return None
        if len(candidates) != 1:
            raise RuntimeError(f'Expected one {platform} manifest for {reference}, found {len(candidates)}')
        descriptor_digest = candidates[0]['digest']
        found = read_manifest(pin_reference(reference, descriptor_digest))
        if found['digest'] != descriptor_digest:
            raise RuntimeError('Registry descriptor digest does not match manifest: ' + reference)
    if 'config' not in found['manifest']:
        raise RuntimeError('Expected a single-platform image manifest: ' + reference)
    if verify_platform:
        raw_config = registry_query(pin_reference(reference, found['digest']),
                                    '--format', '{{json .Image}}')
        try:
            config = json.loads(raw_config)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError('Invalid image configuration: ' + reference) from None
        if not isinstance(config, dict) or config.get('os') != wanted[0] or (
            config.get('architecture') != wanted[1]
        ) or (len(wanted) == 3 and config.get('variant') != wanted[2]):
            raise RuntimeError('Image configuration does not match ' + platform + ': ' + reference)
    return found


def plan_digest_sync(all_entries, registry, namespace, requested='', force_all=False):
    selected, skipped = [], []
    for entry, destination in destination_pairs(all_entries, registry, namespace, requested, force_all):
        source, platform, _, _ = image_parts(entry)
        platform = platform or 'linux/amd64'
        source_manifest = resolve_manifest(source, platform, verify_platform=True)
        target_manifest = None if force_all else resolve_manifest(destination, platform, missing_ok=True)
        source_digest = source_manifest['digest']
        target_digest = target_manifest['digest'] if target_manifest else None
        reason = ('force' if force_all else 'missing' if target_manifest is None else
                  'equal' if source_digest == target_digest else 'changed')
        record = {'source': source, 'entry': entry, 'destination': destination,
                  'platform': platform, 'source_digest': source_digest,
                  'target_digest': target_digest, 'pinned_source': pin_reference(source, source_digest),
                  'reason': reason}
        (skipped if reason == 'equal' else selected).append(record)
    return selected, skipped


def digest_main(images_file=Path('images.txt')):
    registry, namespace = os.environ['ALIYUN_REGISTRY'], os.environ['ALIYUN_NAME_SPACE']
    if not registry or not namespace:
        raise ValueError('ALIYUN_REGISTRY and ALIYUN_NAME_SPACE must be configured.')
    selected, skipped = plan_digest_sync(entries(Path(images_file).read_text()), registry, namespace,
        requested=os.environ.get('INPUT_IMAGES', ''),
        force_all=os.environ.get('INPUT_SYNC_ALL', '').lower() == 'true')
    # Publish nothing until every comparison has succeeded.
    Path(os.environ['SYNC_PLAN_FILE']).write_text(json.dumps(
        {'selected': selected, 'skipped': skipped}, indent=2) + '\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'count={len(selected)}\nskipped={len(skipped)}\n')
    for record in skipped + selected:
        print('PLAN ' + json.dumps(record, sort_keys=True), flush=True)
    print(f'Candidates: {len(selected) + len(skipped)}; equal digests: {len(skipped)}; '
          f'to sync: {len(selected)}', flush=True)


def sync_plan(plan_file):
    plan = json.loads(Path(plan_file).read_text())
    authfile = Path(os.environ.get('DOCKER_CONFIG', str(Path.home() / '.docker'))) / 'config.json'
    for record in plan['selected']:
        destination, expected = record['destination'], record['source_digest']
        print(f'SYNC {record["reason"]}: {record["pinned_source"]} -> {destination}', flush=True)
        subprocess.run(['skopeo', 'copy', '--preserve-digests', '--retry-times', '3',
                        '--authfile', str(authfile), 'docker://' + record['pinned_source'],
                        'docker://' + destination], check=True)
        target = resolve_manifest(destination, record['platform'], verify_platform=True)
        if target['digest'] != expected:
            raise RuntimeError(f'Post-copy digest mismatch for {destination}: '
                               f'expected {expected}, got {target["digest"]}')
        print('VERIFIED ' + json.dumps({'destination': destination, 'platform': record['platform'],
                                       'digest': expected}, sort_keys=True), flush=True)


def main(images_file=Path('images.txt')):
    registry = os.environ['ALIYUN_REGISTRY']
    namespace = os.environ['ALIYUN_NAME_SPACE']
    if not registry or not namespace:
        raise ValueError('ALIYUN_REGISTRY and ALIYUN_NAME_SPACE must be configured.')
    force_all = os.environ.get('INPUT_SYNC_ALL', '').lower() == 'true'
    selected, skipped = plan_sync(
        entries(Path(images_file).read_text()), registry, namespace,
        requested=os.environ.get('INPUT_IMAGES', ''), force_all=force_all,
    )
    # Publish only a COMPLETE plan. A failed check must never cause image pulls.
    Path(os.environ['SELECTED_IMAGES_FILE']).write_text(
        ''.join(entry + '\t' + destination + '\n' for entry, destination in selected)
    )
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'count={len(selected)}\nskipped={len(skipped)}\n')
    for entry, destination in skipped:
        print(f'SKIP existing: {destination} <- {entry}')
    for entry, destination in selected:
        print(f'{"FORCE" if force_all else "SYNC missing"}: {destination} <- {entry}')
    print(f'Candidates: {len(selected) + len(skipped)}; already present: {len(skipped)}; '
          f'to sync: {len(selected)}')


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--compare-digests']:
            digest_main()
        elif sys.argv[1:] == ['--sync-plan']:
            sync_plan(os.environ['SYNC_PLAN_FILE'])
        elif not sys.argv[1:]:
            main()
        else:
            raise ValueError('Expected --compare-digests or --sync-plan')
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
