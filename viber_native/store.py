"""Atomic storage for selected Viber chats; isolated from prices and auth secrets."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .db import initialize

SCHEMA = """
CREATE TABLE IF NOT EXISTS viber_chats (
 account_key TEXT NOT NULL, chat_id INTEGER NOT NULL, name TEXT NOT NULL,
 cursor INTEGER NOT NULL DEFAULT 0 CHECK(cursor>=0), last_collected_at TEXT,
 PRIMARY KEY(account_key,chat_id)
);
CREATE TABLE IF NOT EXISTS viber_messages (
 account_key TEXT NOT NULL, event_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 token TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, direction INTEGER NOT NULL,
 message_type INTEGER NOT NULL, body TEXT NOT NULL, source_status TEXT NOT NULL,
 content_hash TEXT NOT NULL, revision INTEGER NOT NULL,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 PRIMARY KEY(account_key,event_id),
 FOREIGN KEY(account_key,chat_id) REFERENCES viber_chats(account_key,chat_id)
);
CREATE INDEX IF NOT EXISTS idx_viber_chat_time ON viber_messages(account_key,chat_id,timestamp_ms);
CREATE TABLE IF NOT EXISTS viber_message_revisions (
 account_key TEXT NOT NULL, event_id INTEGER NOT NULL, revision INTEGER NOT NULL,
 observed_at TEXT NOT NULL, body TEXT NOT NULL, source_status TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 PRIMARY KEY(account_key,event_id,revision),
 FOREIGN KEY(account_key,event_id) REFERENCES viber_messages(account_key,event_id)
);
CREATE TABLE IF NOT EXISTS viber_runs (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
 status TEXT NOT NULL, seen INTEGER NOT NULL, inserted INTEGER NOT NULL,
 updated INTEGER NOT NULL, error_code TEXT
);
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def account_key(profile_path):
    return hashlib.sha256(profile_path.replace('\\', '/').casefold().encode()).hexdigest()[:24]


def setup(path):
    initialize(path)
    with closing(sqlite3.connect(path)) as con:
        con.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + '\nCOMMIT;')
        with con:
            con.execute("INSERT OR IGNORE INTO sources(id,name,kind,status,notes) VALUES(?,?,?,?,?)",
                        ('viber', 'Viber Desktop', 'api', 'enabled', 'Native read-only collector; selected chats only.'))


def read_cursor(path, key, chat_ids):
    with closing(sqlite3.connect(path)) as con:
        cursors = dict(con.execute('SELECT chat_id,cursor FROM viber_chats WHERE account_key=?', (key,)))
    return min(cursors.get(chat_id, 0) for chat_id in chat_ids)


def integer(value, field, minimum=0):
    if isinstance(value, bool) or not str(value).lstrip('-').isdigit():
        raise ValueError(f'invalid_{field}')
    result = int(value)
    if result < minimum or result > 9223372036854775807:
        raise ValueError(f'invalid_{field}')
    return result


def validate_batch(batch, config, after_id):
    if batch.get('status') != 'ok' or not isinstance(batch.get('profile_path'), str):
        raise ValueError('invalid_batch')
    key = account_key(batch['profile_path'])
    if key != config['account_key']:
        raise ValueError('profile_changed')
    upper = integer(batch['upper_id'], 'upper_id')
    if upper < after_id:
        raise ValueError('source_database_reset')
    selected = {g['chat_id']: g['name'] for g in config['groups']}
    chats = batch.get('chats', [])
    actual = {integer(c['chat_id'], 'chat_id'): c['name'] for c in chats}
    if len(chats) != len(selected) or actual != selected:
        raise ValueError('chat_identity_changed')
    rows, recent = batch.get('rows'), batch.get('recent', [])
    if not isinstance(rows, list) or not isinstance(recent, list):
        raise ValueError('invalid_rows')
    if len(rows) > config['batch_size'] or len(recent) > len(selected) * config['recent_limit']:
        raise ValueError('batch_limit_exceeded')
    normalized = {}
    last = after_id
    for index, row in enumerate(rows + recent):
        event_id = integer(row['event_id'], 'event_id', 1)
        chat_id = integer(row['chat_id'], 'chat_id', 1)
        if chat_id not in selected or event_id > upper:
            raise ValueError('unexpected_message')
        if index < len(rows):
            if event_id <= last:
                raise ValueError('unordered_batch')
            last = event_id
        body = row['body']
        if not isinstance(body, str) or len(body) > 65536:
            raise ValueError('invalid_message_body')
        token = row['token']
        if not isinstance(token, str) or not token.lstrip('-').isdigit():
            raise ValueError('invalid_message_token')
        record = dict(event_id=event_id, chat_id=chat_id, token=token,
                      timestamp_ms=integer(row['timestamp'], 'timestamp'),
                      direction=integer(row['direction'], 'direction'),
                      message_type=integer(row['message_type'], 'message_type'),
                      body=body, source_status=str(row['status']))
        record['content_hash'] = hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if event_id in normalized and normalized[event_id] != record:
            raise ValueError('inconsistent_snapshot')
        normalized[event_id] = record
    cursor = upper if len(rows) < config['batch_size'] else last
    return key, selected, list(normalized.values()), cursor


def save_batch(path, batch, config, after_id, started_at):
    key, selected, rows, cursor = validate_batch(batch, config, after_id)
    now = utc_now()
    inserted = updated = 0
    with closing(sqlite3.connect(path)) as con:
        con.execute('PRAGMA foreign_keys=ON')
        with con:
            for chat_id, name in selected.items():
                con.execute('INSERT INTO viber_chats(account_key,chat_id,name) VALUES(?,?,?) ON CONFLICT DO NOTHING',
                            (key, chat_id, name))
            for row in rows:
                old = con.execute('SELECT token,content_hash,revision,chat_id FROM viber_messages WHERE account_key=? AND event_id=?',
                                  (key, row['event_id'])).fetchone()
                if old and (old[0] != row['token'] or old[3] != row['chat_id']):
                    raise ValueError('event_id_reused')
                changed = not old or old[1] != row['content_hash']
                if not changed:
                    con.execute('UPDATE viber_messages SET last_seen_at=? WHERE account_key=? AND event_id=?',
                                (now, key, row['event_id']))
                    continue
                revision = old[2] + 1 if old else 1
                if old:
                    updated += 1
                    con.execute('UPDATE viber_messages SET timestamp_ms=?,direction=?,message_type=?,body=?,source_status=?,content_hash=?,revision=?,last_seen_at=? WHERE account_key=? AND event_id=?',
                                (row['timestamp_ms'],row['direction'],row['message_type'],row['body'],row['source_status'],row['content_hash'],revision,now,key,row['event_id']))
                else:
                    inserted += 1
                    con.execute('INSERT INTO viber_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                (key,row['event_id'],row['chat_id'],row['token'],row['timestamp_ms'],row['direction'],row['message_type'],row['body'],row['source_status'],row['content_hash'],revision,now,now))
                con.execute('INSERT INTO viber_message_revisions VALUES(?,?,?,?,?,?,?)',
                            (key,row['event_id'],revision,now,row['body'],row['source_status'],row['content_hash']))
            for chat_id in selected:
                con.execute('UPDATE viber_chats SET cursor=max(cursor,?),last_collected_at=? WHERE account_key=? AND chat_id=?',
                            (cursor,now,key,chat_id))
            con.execute('INSERT INTO viber_runs(started_at,finished_at,status,seen,inserted,updated) VALUES(?,?,?,?,?,?)',
                        (started_at,now,'success',len(rows),inserted,updated))
    return dict(status='success', seen=len(rows), inserted=inserted, updated=updated, cursor=cursor)


def record_failure(path, started_at, status, error_code):
    with closing(sqlite3.connect(path)) as con, con:
        con.execute('INSERT INTO viber_runs(started_at,finished_at,status,seen,inserted,updated,error_code) VALUES(?,?,?,0,0,0,?)',
                    (started_at,utc_now(),status,error_code))
