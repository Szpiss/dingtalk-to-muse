#!/usr/bin/env python3
"""Read macOS notification snapshots. No service installation or forwarding.

Discovers the macOS Notification Center SQLite database, snapshots it safely,
and decodes notification payloads (NSKeyedArchiver) into plain dicts.
Standard library only.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import time

APPLE_EPOCH = 978307200


def resolve_bundle_id(spec):
    """Accept a bundle identifier (e.g. com.example.App) or a path to an .app bundle."""
    candidate = Path(spec).expanduser()
    if candidate.exists():
        info = candidate / 'Contents/Info.plist'
        if not info.is_file():
            raise ValueError(f'{spec}: not an .app bundle (no Contents/Info.plist)')
        with info.open('rb') as f:
            return plistlib.load(f)['CFBundleIdentifier']
    if '.' in spec and ' ' not in spec and '/' not in spec:
        return spec
    raise ValueError(f'{spec}: give a bundle identifier or a path to an .app bundle')


def discover():
    home = Path.home()
    candidates = [
        home / 'Library/Group Containers/group.com.apple.usernoted/db2/db',
        home / 'Library/Group Containers/group.com.apple.notificationcenter/db2/db',
    ]
    darwin = subprocess.check_output(['getconf', 'DARWIN_USER_DIR'], text=True).strip()
    candidates += [Path(darwin) / 'com.apple.notificationcenter/db2/db',
                   Path(darwin) / 'com.apple.notificationcenter/db/db']
    legacy = home / 'Library/Application Support/NotificationCenter'
    if legacy.exists():
        candidates += sorted(legacy.glob('*/db')) + sorted(legacy.glob('*.db'))
    found = [p for p in candidates if p.is_file()]
    if not found:
        raise FileNotFoundError('No notification DB found; checked: ' + ', '.join(map(str, candidates)))
    return found[0]


@contextlib.contextmanager
def snapshot(source):
    """Copy main DB and WAL to a private /tmp directory; never connect to source."""
    with tempfile.TemporaryDirectory(prefix='notif-dump-', dir='/tmp') as tmp:
        dest = Path(tmp) / 'db'
        for suffix in ('', '-wal', '-shm'):
            src = Path(str(source) + suffix)
            if src.exists():
                result = subprocess.run(['/bin/cp', str(src), str(dest) + suffix],
                                        capture_output=True, text=True)
                if result.returncode:
                    raise PermissionError(result.stderr.strip())
                os.chmod(str(dest) + suffix, 0o600)
        conn = sqlite3.connect(dest.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Copied notification DB failed quick_check; retry snapshot')
            yield conn
        finally:
            conn.close()


def unarchive(obj):
    if not isinstance(obj, dict) or '$objects' not in obj:
        return obj
    objects = obj['$objects']

    def resolve(value, visiting=frozenset()):
        if isinstance(value, plistlib.UID):
            index = value.data
            if index in visiting:
                return None
            return resolve(objects[index], visiting | {index})
        if isinstance(value, dict):
            if 'NS.keys' in value and 'NS.objects' in value:
                return dict(zip(resolve(value['NS.keys'], visiting),
                                resolve(value['NS.objects'], visiting)))
            return {k: resolve(v, visiting) for k, v in value.items() if k != '$class'}
        if isinstance(value, list):
            return [resolve(v, visiting) for v in value]
        return value

    top = obj.get('$top', {})
    root = top.get('root', next(iter(top.values()), None))
    if root is None:
        raise ValueError('NSKeyedArchiver has no root')
    return resolve(root)


def notification(row):
    rowid, app, delivered, blob = row
    payload = unarchive(plistlib.loads(blob))
    if not isinstance(payload, dict):
        raise ValueError(f'rowid={rowid}: unsupported notification payload')
    req = payload.get('req', payload)
    if not isinstance(req, dict):
        raise ValueError(f'rowid={rowid}: request is not a dictionary')

    def field(*keys):
        for key in keys:
            if key in req:
                value = req[key]
                if value is None:
                    return ''
                if not isinstance(value, str):
                    raise ValueError(f'rowid={rowid}: {key} is {type(value).__name__}')
                return value
        return ''

    timestamp = delivered if delivered is not None else payload.get('date')
    if not isinstance(timestamp, (float, int)):
        raise ValueError(f'rowid={rowid}: notification timestamp missing')
    unix = timestamp + APPLE_EPOCH
    return {'ts': unix, 'date': dt.datetime.fromtimestamp(unix).astimezone().isoformat(),
            'app': app or payload.get('app', ''),
            'title': field('titl', 'title', 'NSTitle'),
            'subtitle': field('subt', 'subtitle', 'NSSubtitle'),
            'body': field('body', 'NSInformativetext', 'NSInformativeText'), 'rowid': rowid}


def read(source, limit=20, after=None):
    with snapshot(source) as conn:
        sql = ('SELECT r.rowid,a.identifier,r.delivered_date,r.data '
               'FROM record r LEFT JOIN app a ON a.app_id=r.app_id ')
        if after is None:
            rows = conn.execute(sql + 'ORDER BY r.rowid DESC LIMIT ?', (limit,)).fetchall()
        else:
            rows = conn.execute(sql + 'WHERE r.rowid > ? ORDER BY r.rowid', (after,)).fetchall()
        high = conn.execute('SELECT COALESCE(MAX(rowid),0) FROM record').fetchone()[0]
        return [notification(row) for row in rows], high


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True,
                        help='Bundle identifier (e.g. com.example.App) or path to an .app bundle to filter/verify')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--watch-seconds', type=int, default=0)
    parser.add_argument('--marker', default='notif-forward-test')
    parser.add_argument('--after-rowid', type=int)
    args = parser.parse_args()
    os.umask(0o077)
    source = args.db or discover()
    target = resolve_bundle_id(args.app)
    print(json.dumps({'db': str(source), 'target_bundle': target}, ensure_ascii=False), file=sys.stderr, flush=True)
    items, high = read(source, args.limit)
    for item in items:
        print(json.dumps(item, ensure_ascii=False), flush=True)
    print(f'Parsed {len(items)} notifications; baseline rowid={high}', file=sys.stderr, flush=True)
    if not args.watch_seconds:
        return 0
    if not 1 <= args.watch_seconds <= 300:
        raise ValueError('Verification window must be 1..300 seconds')
    after = high if args.after_rowid is None else args.after_rowid
    deadline = time.monotonic() + args.watch_seconds
    while True:
        items, high = read(args.db or discover(), after=after)
        if high < after:
            raise RuntimeError('Notification rowid reset during verification')
        for item in items:
            # During verification, print only new records from the target app to reduce noise.
            if item['app'].casefold() == target.casefold():
                print(json.dumps(item, ensure_ascii=False), flush=True)
                if args.marker in (item['title'] + '\n' + item['subtitle'] + '\n' + item['body']):
                    print('PASS: test marker found in a new notification', file=sys.stderr, flush=True)
                    return 0
        after = max(after, high)
        if time.monotonic() >= deadline:
            print('FAIL: no matching test notification in the verification window. STOP; do not install forwarding.', file=sys.stderr, flush=True)
            return 2
        time.sleep(min(5, max(0, deadline - time.monotonic())))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'LOUD FAIL: {type(exc).__name__}: {exc}\nCheck Full Disk Access for the actual running host/interpreter, DB path and schema.', file=sys.stderr, flush=True)
        sys.exit(1)
