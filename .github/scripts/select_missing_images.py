"""Plan image sync using ACR tag existence, without pulling image layers.

Default runs examine the entire list, including entries from failed older runs.
Docker uses the credentials already supplied by the workflow's ACR login step.
"""
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


def plan_sync(all_entries, registry, namespace, requested='', force_all=False, inspect=None):
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

    inspect = inspect or manifest_exists
    selected, skipped = [], []
    for entry in candidates:
        _, platform, source_namespace, name_tag = image_parts(entry)
        platform_prefix = platform.replace('/', '_') + '_' if platform else ''
        namespace_prefix = source_namespace + '_' if (
            name_tag.split(':', 1)[0] in duplicates and source_namespace
        ) else ''
        destination = (registry.rstrip('/') + '/' + namespace.strip('/') + '/' +
                       platform_prefix + namespace_prefix + name_tag)
        pair = (entry, destination)
        if not force_all and inspect(destination):
            skipped.append(pair)
        else:
            selected.append(pair)
    return selected, skipped


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
        main()
    except (ValueError, RuntimeError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
