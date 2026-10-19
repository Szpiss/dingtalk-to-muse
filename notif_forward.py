#!/usr/bin/env python3
"""Copy new macOS notifications from one app to a private, bounded local JSONL file.

Polls the macOS Notification Center database and appends notifications from the
target app as JSON lines. Safe for concurrent/scheduled runs: file locking,
atomic writes, cursor persisted in state.json, loud failure on any anomaly.
Standard library only.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import tempfile
import time

from notif_dump import discover, notification, resolve_bundle_id, snapshot


def atomic_write(path, text):
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def lock(directory):
    with (directory / 'forward.lock').open('a') as handle:
        os.chmod(handle.name, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def forward(directory, target, source=None, start_after=None):
    source = source or discover()
    info = source.stat()
    identity = {'path': str(source.resolve()), 'device': info.st_dev, 'inode': info.st_ino}
    state_path = directory / 'state.json'
    output_path = directory / 'notifications.jsonl'
    state = json.loads(state_path.read_text()) if state_path.exists() else None
    if state and state['database'] != identity:
        raise RuntimeError('Notification DB identity/path changed; inspect before resetting cursor')
    if state and state['bundle_id'].casefold() != target.casefold():
        raise RuntimeError('Target bundle id changed; inspect before resetting cursor')
    if state and start_after is not None:
        raise ValueError('--start-after is only valid on first initialization')
    existing = []
    if output_path.exists():
        os.chmod(output_path, 0o600)
        for line in output_path.read_text(encoding='utf-8').splitlines():
            item = json.loads(line)
            if not isinstance(item['rowid'], int) or item['app'].casefold() != target.casefold():
                raise ValueError('Unexpected rowid/app in notifications.jsonl')
            existing.append(item)
        ids = [item['rowid'] for item in existing]
        if ids != sorted(set(ids)):
            raise ValueError('notifications.jsonl has duplicate or unordered rowids')
    with snapshot(source) as conn:
        high = conn.execute('SELECT COALESCE(MAX(rowid),0) FROM record').fetchone()[0]
        last = state['last_rowid'] if state else (high if start_after is None else start_after)
        if not isinstance(last, int) or last < 0:
            raise ValueError('Invalid last_rowid')
        # Output is committed first. If interrupted before state commit, recover
        # the cursor from already committed rows rather than append duplicates.
        last = max(last, max((x['rowid'] for x in existing), default=0))
        if high < last:
            raise RuntimeError(f'Notification rowid reset/pruned below cursor: high={high}, last={last}')
        rows = conn.execute(
            'SELECT r.rowid,a.identifier,r.delivered_date,r.data FROM record r '
            'JOIN app a USING(app_id) WHERE r.rowid>? AND lower(a.identifier)=? '
            'ORDER BY r.rowid', (last, target.casefold())).fetchall()
        new = []
        for row in rows:
            parsed = notification(row)
            new.append({key: parsed[key] for key in ('ts', 'app', 'title', 'body', 'rowid')})
    after = source.stat()
    if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError('Notification DB replaced while snapshot was being read')
    retained = (existing + new)[-200:]
    if new or len(existing) > 200 or not output_path.exists():
        atomic_write(output_path, ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in retained))
    now = time.time()
    next_state = {
        'last_rowid': high, 'database': identity, 'bundle_id': target,
        'started_at': state['started_at'] if state else now,
        'last_success_at': now,
        'successful_runs': (state.get('successful_runs', 0) if state else 0) + 1,
        'total_forwarded': (state.get('total_forwarded', 0) if state else 0) + len(new),
    }
    atomic_write(state_path, json.dumps(next_state, ensure_ascii=False, indent=2) + '\n')
    return {'new': len(new), 'retained': len(retained), 'last_rowid': high}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True,
                        help='Bundle identifier (e.g. com.example.App) or path to an .app bundle to forward')
    parser.add_argument('--data-dir', type=Path, default=Path.home() / '.notif-forward')
    parser.add_argument('--start-after', type=int, help='First run only: include rows after this verified baseline')
    args = parser.parse_args()
    os.umask(0o077)
    target = resolve_bundle_id(args.app)
    directory = args.data_dir.expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    log_path = directory / 'forward.log'
    handler = RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=2, encoding='utf-8')
    os.chmod(log_path, 0o600)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[handler])
    try:
        with lock(directory):
            result = forward(directory, target, start_after=args.start_after)
        logging.info('OK %s', json.dumps(result))
        print(json.dumps(result), flush=True)
        return 0
    except Exception as exc:
        logging.exception('LOUD FAIL: %s', exc)
        print(f'LOUD FAIL: {type(exc).__name__}: {exc}. See {log_path}; check Full Disk Access, DB path/schema and state.', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
