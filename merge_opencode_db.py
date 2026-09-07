#!/usr/bin/env python3
"""Merge a (local machine's) opencode.db into the server's opencode.db so
ccusage counts both machines' OpenCode sessions together.

Usage (on the server):
    python3 merge_opencode_db.py [local_db_copy] [server_db]

Defaults:
    local_db_copy = ~/opencode-local.db
    server_db     = ~/.local/share/opencode/opencode.db

The merge is additive only (INSERT OR IGNORE, UUID primary keys never
collide), the server db is backed up first, and config/credential tables are
skipped. Re-running is a harmless no-op.
"""
import os
import shutil
import sqlite3
import sys
import time

# Tables that identify machines/accounts rather than usage — never merged.
SKIP_TABLES = {
    'migration', 'data_migration', 'event_sequence', 'event',
    'account', 'account_state', 'control_account', 'credential',
}


def merge(local_db, server_db):
    if not os.path.isfile(local_db):
        sys.exit(f"local db not found: {local_db}")
    backup = server_db + '.bak-' + str(int(time.time()))
    shutil.copy(server_db, backup)
    print(f"backup: {backup}")

    con = sqlite3.connect(server_db)
    con.execute("ATTACH ? AS local_db", (local_db,))
    tables = [r[0] for r in con.execute(
        "SELECT name FROM local_db.sqlite_master WHERE type='table'")]
    total = 0
    for t in sorted(tables):
        if t in SKIP_TABLES:
            continue
        cols = [r[1] for r in con.execute(f"PRAGMA local_db.table_info({t})")]
        if not cols:
            continue
        try:
            collist = ', '.join(f'"{c}"' for c in cols)
            cur = con.execute(
                f"INSERT OR IGNORE INTO \"{t}\" ({collist}) "
                f"SELECT {collist} FROM local_db.\"{t}\"")
            if cur.rowcount > 0:
                print(f"  {t}: +{cur.rowcount}")
                total += cur.rowcount
        except sqlite3.Error as e:
            print(f"  {t}: skipped ({e})")
    con.commit()
    con.close()
    print(f"merged rows total: {total}")


if __name__ == '__main__':
    local = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~/opencode-local.db')
    server = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser(
        '~/.local/share/opencode/opencode.db')
    merge(local, server)
