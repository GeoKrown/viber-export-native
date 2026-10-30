"""Download selected Viber photo originals without showing chats or clicking.

Uses the running application's own requestPayload operation. Text and images
are not sent to an external parser. A successful command is verified in SQL.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

from .collector import native_read, atomic_json, process_lock
from .store import account_key
from .files import local_image
from .collector import CONFIG, ROOT, load_config
PRIVATE = ROOT / "data" / "private"


def metadata(chat, profile, events, reader):
    result = reader(dict(mode='media', chat_ids=[int(chat['chat_id'])], event_ids=events,
                         after_id='0', batch_size=100, recent_limit=0, wait_seconds=20))
    if result.get('status') != 'ok':
        raise RuntimeError('media_metadata_unavailable')
    if account_key(result['profile_path']) != profile or result.get('chats') != [
            dict(chat_id=str(chat['chat_id']), name=chat['name'])]:
        raise ValueError('media_identity_changed')
    rows = result['rows']
    if len(rows) != len(events) or {int(x['event_id']) for x in rows} != set(events):
        raise ValueError('media_events_missing')
    if any(str(x['chat_id']) != str(chat['chat_id']) or int(x['message_type']) != 2 for x in rows):
        raise ValueError('media_wrong_chat_or_type')
    if type(result.get('process_id')) is not int or result['process_id'] <= 0:
        raise ValueError('media_process_missing')
    return result


def original(row, roots):
    from PIL import Image
    try:
        path = local_image(row, roots)
        with Image.open(path) as image:
            width, height = image.size
            image.verify()
        return dict(path=str(path), width=width, height=height,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    except (OSError, ValueError):
        return None


def download_originals(chat, profile, events, *, reader=native_read, roots=None,
                       polls=3, sleeper=time.sleep):
    if (not isinstance(events, list) or not 1 <= len(events) <= 20 or
        len(set(events)) != len(events) or any(type(x) is not int or not 1 <= x <= 9007199254740991 for x in events)):
        raise ValueError('invalid_media_events')
    if type(polls) is not int or not 1 <= polls <= 3:
        raise ValueError('invalid_poll_limit')
    roots = roots if roots is not None else [Path.home() / 'Documents' / 'ViberDownloads']
    before = metadata(chat, profile, events, reader)
    identity = {x['event_id']: (x['token'], x['timestamp']) for x in before['rows']}
    missing = [int(x['event_id']) for x in before['rows'] if original(x, roots) is None]
    if missing:
        command = reader(dict(mode='media-request', chat_ids=[int(chat['chat_id'])],
                              event_ids=missing, expected_pid=before['process_id'],
                              after_id='0', batch_size=100, recent_limit=0, wait_seconds=5))
        if (command.get('status') != 'ok' or command.get('process_id') != before['process_id'] or
            command.get('requested') != missing or command.get('chat_unchanged') is not True):
            raise RuntimeError('media_command_unconfirmed')
    after = before
    for attempt in range(polls if missing else 1):
        if missing:
            sleeper(1)
            after = metadata(chat, profile, events, reader)
            if after['process_id'] != before['process_id'] or any(
                identity[x['event_id']] != (x['token'], x['timestamp']) for x in after['rows']):
                raise ValueError('media_event_changed')
        items = [dict(event_id=x['event_id'], original=original(x, roots)) for x in after['rows']]
        if all(x['original'] is not None for x in items):
            break
    return dict(complete=all(x['original'] is not None for x in items),
                requested=missing, items=items, rows=after['rows'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--chat-id', type=int, required=True)
    parser.add_argument('--event-id', type=int, action='append', required=True)
    parser.add_argument('--downloads', type=Path, default=Path.home()/'Documents'/'ViberDownloads')
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        matches = [g for g in cfg['groups'] if g['chat_id'] == args.chat_id]
        if len(matches) != 1:
            raise ValueError('chat_not_configured')
        group = matches[0]
        with process_lock(PRIVATE/'media-download.lock'):
            result = download_originals(dict(chat_id=str(group['chat_id']), name=group['name']),
                                        cfg['account_key'], args.event_id, roots=[args.downloads])
            report = PRIVATE/f'download-{group["chat_id"]}.json'
            atomic_json(report, result)
        print(json.dumps(dict(complete=result['complete'], requested=result['requested'],
                             items=result['items'], report=str(report)), ensure_ascii=True))
        return 0 if result['complete'] else 2
    except (RuntimeError, ValueError, OSError, KeyError, TypeError):
        print(json.dumps(dict(status='error', error='download_not_verified')))
        return 1

