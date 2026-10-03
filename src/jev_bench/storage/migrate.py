"""Restartable, byte-preserving migration of existing repository datasets."""
import argparse
from contextlib import ExitStack
import csv
import hashlib
import json
from pathlib import Path
import shutil

from jev_bench.storage.io import atomic_write, output_lock
from jev_bench.storage.paths import PROJECT_ROOT

TRANSIENT_LOCKS = {'.benchmark.lock', '.jev-budget.lock'}


def durable_inventory(files):
    """Keep original journal hashes while excluding ephemeral writer locks."""
    return {name: digest for name, digest in files.items()
            if Path(name).name not in TRANSIENT_LOCKS}


def inventory(path):
    paths = [path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file())
    return {str(p.relative_to(path)) if path.is_dir() else '.':
            hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.name != '.DS_Store'}


def entries(root):
    moves = []
    for suite in ('benchmark', 'hard_case'):
        moves.append((f'results/{suite}', f'results/experiments/baseline/{suite}'))
    for name, target in [('jev_30', 'tev_30'), ('gemma_30', 'gemma_30'),
                         ('jev_api_10', 'jev_api_30'), ('jev_api_1', 'jev_api_1'),
                         ('prefix_pilot', 'prefix_pilot')]:
        moves.append((f'results/{name}', f'results/experiments/{target}'))
    old = root / 'hard_case/results'
    if old.exists():
        moves.extend((str(p.relative_to(root)), f'results/historical/{p.name}/hard_case')
                     for p in sorted(old.iterdir()) if p.is_dir())
    moves.append(('results_contaminated_dont_use', 'results/quarantine/contaminated_dont_use'))
    if (root / 'results').exists():
        moves.extend((str(p.relative_to(root)), f'results/experiments/baseline/{p.name}')
                     for p in sorted((root / 'results').iterdir())
                     if p.is_file() and p.name != '.DS_Store')
    return [{'source': source, 'destination': dest, 'sha256': inventory(root / source)}
            for source, dest in moves if (root / source).exists()]


def destination_path(root, item, physical_root=None):
    staged = physical_root / item['destination'] if physical_root else None
    return staged if staged is not None and staged.exists() else root / item['destination']


def relocation_paths(root, moves, physical_root=None):
    mappings = {}
    for item in moves:
        destination = destination_path(root, item, physical_root)
        if not destination.is_dir():
            continue
        mappings[str(root / item['source'])] = destination
        # Retain prefixes actually recorded before a checkout itself was relocated.
        for history in destination.rglob('attempt_history.csv'):
            for row in csv.DictReader(history.open(newline='', encoding='utf-8')):
                value = row.get('audit_path')
                if value and Path(value).is_absolute():
                    mappings[str(Path(value).parent)] = history.parent / 'execution_audit'
    return dict(sorted(mappings.items(), key=lambda pair: len(Path(pair[0]).parts), reverse=True))


def evidence_status(root, moves, physical_root=None):
    """Check every retained audit/log reference without changing its serialized value."""
    mappings = [(Path(old), new) for old, new in relocation_paths(root, moves, physical_root).items()]
    issues = []
    linked = 0
    def resolve(value, suite):
        p = Path(value)
        if not p.is_absolute():
            return suite / p
        for old, new in mappings:
            if p.is_relative_to(old):
                return new / p.relative_to(old)
        return p
    for item in moves:
        destination = destination_path(root, item, physical_root)
        histories = destination.rglob('attempt_history.csv') if destination.is_dir() else []
        for history in histories:
            for row in csv.DictReader(history.open(newline='', encoding='utf-8')):
                raw = json.loads(row.get('raw_response_json') or '{}')
                raw = raw if isinstance(raw, dict) else {}
                link = raw.get('execution_audit')
                if not link:
                    issues.append({'dataset': str(history.parent), 'reason': 'missing linked audit'})
                    continue
                audit_path = resolve(link['audit_path'], history.parent)
                if not audit_path.exists() or hashlib.sha256(audit_path.read_bytes()).hexdigest() != link.get('audit_sha256'):
                    issues.append({'dataset': str(history.parent), 'call_id': row.get('call_id'), 'reason': 'missing or mismatched audit'})
                    continue
                audit = json.loads(audit_path.read_text())
                if audit.get('log_path') and audit.get('log_sha256'):
                    log = resolve(audit['log_path'], history.parent)
                    if not log.exists() or hashlib.sha256(log.read_bytes()).hexdigest() != audit['log_sha256']:
                        issues.append({'dataset': str(history.parent), 'call_id': row.get('call_id'), 'reason': 'missing or mismatched log'})
                        continue
                linked += 1
    grouped = {}
    for issue in issues:
        key = (issue['dataset'], issue['reason'])
        group = grouped.setdefault(key, {'dataset': issue['dataset'], 'reason': issue['reason'], 'attempts': 0})
        group['attempts'] += 1
    return {'linked_attempts': linked, 'issues': list(grouped.values())}


def migrate(root=PROJECT_ROOT, *, apply=False):
    root = Path(root).resolve()
    manifest_path = root / 'results/migrations/migration.json'
    if manifest_path.exists():
        plan = json.loads(manifest_path.read_text())
        if plan['status'] == 'complete':
            for item in plan['moves']:
                destination = root / item['destination']
                if destination.name in TRANSIENT_LOCKS:
                    continue
                # Quarantine is intentionally local-only and absent in Git checkouts.
                if destination.is_relative_to(root / 'results/quarantine') and not destination.exists():
                    continue
                if durable_inventory(inventory(destination)) != durable_inventory(item['sha256']):
                    raise ValueError('Migrated destination changed: ' + item['destination'])
            return plan
    else:
        plan = {'version': 1, 'status': 'planned', 'moves': entries(root)}
    if not apply:
        return plan
    migration_dir = manifest_path.parent
    migration_dir.mkdir(parents=True, exist_ok=True)
    stage = root / 'results/.migration_staging'
    backup = root / 'results/.migration_backup/original'
    with output_lock(migration_dir), ExitStack() as stack:
        # Respect orchestration, budget, and suite writers before snapshotting.
        from jev_bench.storage.locking import fcntl
        for item in plan['moves']:
            source = root / item['source']
            if source.is_dir():
                for lock in sorted(p for p in source.rglob('*') if p.is_file() and p.name in {'.benchmark.lock', '.jev-budget.lock'}):
                    handle = stack.enter_context(lock.open('r'))
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise ValueError('Dataset writer is active: ' + str(lock)) from exc
        # Reject collisions before moving any original files.
        for item in plan['moves']:
            source, destination = root / item['source'], root / item['destination']
            if destination.exists() and item.get('published') is not True:
                if source.exists() or inventory(destination) != item['sha256']:
                    raise ValueError('Conflicting migration destination: ' + str(destination))
        atomic_write(manifest_path, lambda handle: json.dump(plan, handle, indent=2))
        # Stage and verify the whole migration before relocating any originals.
        for item in plan['moves']:
            destination = root / item['destination']
            if destination.exists():
                continue
            source = root / item['source']
            saved = backup / item['source']
            origin = source if source.exists() else saved
            if not origin.exists() or inventory(origin) != item['sha256']:
                raise ValueError('Migration source fingerprint mismatch: ' + str(origin))
            staged = stage / item['destination']
            if staged.exists() and inventory(staged) != item['sha256']:
                shutil.rmtree(staged) if staged.is_dir() else staged.unlink()
            if not staged.exists():
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(origin, staged) if origin.is_dir() else shutil.copy2(origin, staged)
            if inventory(staged) != item['sha256']:
                raise ValueError('Staged copy fingerprint mismatch')
        staged_evidence = evidence_status(root, plan['moves'], stage)
        invalid = [issue for issue in staged_evidence['issues']
                   if '/experiments/' in issue['dataset'] and issue['reason'] != 'missing linked audit']
        if invalid:
            raise ValueError('Staged execution evidence is not resolvable: ' + str(invalid[0]))
        for item in plan['moves']:
            source, destination = root / item['source'], root / item['destination']
            if destination.exists():
                if inventory(destination) != item['sha256']:
                    raise ValueError('Published destination fingerprint mismatch')
                item['published'] = True
                continue
            saved = backup / item['source']
            origin = source if source.exists() else saved
            if not origin.exists() or inventory(origin) != item['sha256']:
                raise ValueError('Migration source fingerprint mismatch: ' + str(origin))
            staged = stage / item['destination']
            if staged.exists() and inventory(staged) != item['sha256']:
                if staged.is_dir():
                    shutil.rmtree(staged)
                else:
                    staged.unlink()
            if not staged.exists():
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(origin, staged) if origin.is_dir() else shutil.copy2(origin, staged)
            if inventory(staged) != item['sha256']:
                raise ValueError('Staged copy fingerprint mismatch')
            if source.exists():
                saved.parent.mkdir(parents=True, exist_ok=True)
                if saved.exists():
                    raise ValueError('Migration backup already exists: ' + str(saved))
                source.rename(saved)
            destination.parent.mkdir(parents=True, exist_ok=True)
            staged.rename(destination)
            item['published'] = True
            atomic_write(manifest_path, lambda handle: json.dump(plan, handle, indent=2))
        paths = {old: str(new.relative_to(root / 'results'))
                 for old, new in relocation_paths(root, plan['moves']).items()}
        atomic_write(migration_dir / 'relocations.json', lambda h: json.dump({'version': 1, 'paths': paths}, h, indent=2))
        catalog = {'version': 1, 'datasets': {}}
        for item in plan['moves']:
            parts = Path(item['destination']).parts
            dataset = str(Path(*parts[1:3]))
            catalog['datasets'][dataset] = {'archived': True,
                'category': parts[1], 'original_location': item['source']}
        atomic_write(root / 'results/catalog.json', lambda h: json.dump(catalog, h, indent=2))
        plan['evidence'] = evidence_status(root, plan['moves'])
        # Audited experiment datasets must keep all existing evidence resolvable.
        invalid = [issue for issue in plan['evidence']['issues']
                   if '/experiments/' in issue['dataset'] and issue['reason'] != 'missing linked audit']
        if invalid:
            raise ValueError('Migrated execution evidence is not resolvable: ' + str(invalid[0]))
        plan['status'] = 'complete'
        atomic_write(manifest_path, lambda handle: json.dump(plan, handle, indent=2))
        if stage.exists():
            shutil.rmtree(stage)
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    plan = migrate(apply=args.apply)
    print(json.dumps({'status': plan['status'], 'moves': [
        {'source': i['source'], 'destination': i['destination'], 'files': len(i['sha256'])}
        for i in plan['moves']], 'evidence': plan.get('evidence')}, indent=2))


if __name__ == '__main__':
    main()
