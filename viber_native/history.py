"""Export a bounded history window for configured chats."""
import argparse
import time
from pathlib import Path
from .collector import native_read, atomic_json, load_config, CONFIG, ROOT
from .store import account_key

def read_group(chat, profile, cutoff_ms, *, reader=native_read, max_messages=1000):
    rows, seen = [], set()
    before = 9223372036854775807
    for _ in range(max_messages // 200 + 1):
        result = reader(dict(mode='history',chat_ids=[int(chat['chat_id'])],after_id='0',
                             before_id=before,cutoff_ms=cutoff_ms,batch_size=200,
                             recent_limit=0,wait_seconds=45))
        if result.get('status') != 'ok':
            raise RuntimeError('native_read_incomplete')
        if account_key(result['profile_path']) != profile or result.get('chats') != [
                dict(chat_id=str(chat['chat_id']),name=chat['name'])]:
            raise ValueError('group_identity_changed')
        page = result['rows']
        if not isinstance(page,list) or len(page)>200:
            raise ValueError('invalid_page')
        for row in page:
            event = int(row['event_id'])
            if event <= 0 or event >= before or event in seen or str(row['chat_id']) != str(chat['chat_id']):
                raise ValueError('invalid_message_order')
            timestamp = int(row['timestamp'])
            if not cutoff_ms <= timestamp <= 253402214400000:
                raise ValueError('invalid_message_date')
            before = event
            seen.add(event)
            rows.append(row)
        if len(rows)>max_messages:
            raise ValueError('history_limit_exceeded')
        if len(page)<200:
            return sorted(rows,key=lambda r:(int(r['timestamp']),int(r['event_id'])))
    raise ValueError('history_limit_exceeded')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--output', type=Path, default=ROOT/'data'/'private'/'history.json')
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 90:
        parser.error('--days must be between 1 and 90')
    try:
        cfg = load_config(args.config)
        cutoff = int((time.time() - args.days * 86400) * 1000)
        groups = []
        for group in cfg['groups']:
            chat = dict(chat_id=str(group['chat_id']), name=group['name'])
            rows = read_group(chat, cfg['account_key'], cutoff)
            groups.append(dict(chat_id=group['chat_id'], name=group['name'], rows=rows))
        atomic_json(args.output, dict(account_key=cfg['account_key'], cutoff_ms=cutoff, groups=groups))
        print(f'Exported {sum(len(g["rows"]) for g in groups)} messages to {args.output}')
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, TypeError):
        print('History export failed; no complete export was written.')
        return 1

