"""Read selected Viber Desktop groups into viber.sqlite (Windows x64).

python -m viber_native.collector chats
python -m viber_native.collector configure --group "Exact group name"
python -m viber_native.collector run --once
python -m viber_native.collector run
"""
import argparse
from contextlib import contextmanager
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

from .db import DEFAULT_PATH
from . import store

ROOT = Path.cwd()
CONFIG = ROOT / 'data' / 'private' / 'viber_config.json'


def bounded_int(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('invalid_numeric_configuration')
    return value


def load_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if config.get('version') != 1 or not re.fullmatch(r'[a-f0-9]{24}', config.get('account_key', '')):
        raise ValueError('invalid_configuration')
    groups = config.get('groups')
    if not isinstance(groups, list) or not 1 <= len(groups) <= 10:
        raise ValueError('select_between_1_and_10_groups')
    for group in groups:
        bounded_int(group['chat_id'], 1, 2147483647)
        if not isinstance(group['name'], str) or not group['name'].strip():
            raise ValueError('invalid_group_name')
    if len({g['chat_id'] for g in groups}) != len(groups):
        raise ValueError('duplicate_group')
    for key, default, minimum, maximum in [
        ('batch_size',100,1,200), ('recent_limit',100,0,100),
        ('wait_seconds',45,1,55), ('interval_seconds',30,5,3600)]:
        config[key] = bounded_int(config.get(key, default), minimum, maximum)
    return config


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


@contextmanager
def process_lock(path):
    """OS-held lock is released after crashes; never delete another process's lock."""
    import msvcrt
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('collector_already_running') from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def native_read(request):
    try:
        completed = subprocess.run([sys.executable, '-m', 'viber_native.worker'],
                                   cwd=ROOT, input=json.dumps(request), text=True,
                                   encoding='utf-8', capture_output=True,
                                   timeout=request['wait_seconds'] + 20)
    except subprocess.TimeoutExpired:
        return {'status':'unavailable','error':'worker_timeout'}
    if completed.returncode or len(completed.stdout) > 16 * 1024 * 1024:
        return {'status':'error','error':'worker_failed'}
    try:
        result = json.loads(completed.stdout)
        if not isinstance(result, dict) or result.get('status') not in ('ok','idle','unavailable','error'):
            raise ValueError
        return result
    except ValueError:
        return {'status':'error','error':'invalid_worker_response'}


def configure(path, names):
    result = native_read(dict(mode='list',chat_ids=[],after_id='0',batch_size=100,wait_seconds=45))
    if result['status'] != 'ok':
        raise RuntimeError(result.get('error', 'discovery_failed'))
    if not names:
        return dict(account_key=store.account_key(result['profile_path']), chats=result['chats'])
    groups = []
    for name in names:
        matches = [c for c in result['chats'] if c['name'].casefold() == name.casefold()]
        if len(matches) != 1:
            raise ValueError('group_missing_or_ambiguous')
        groups.append(dict(chat_id=int(matches[0]['chat_id']), name=matches[0]['name']))
    config = dict(version=1,account_key=store.account_key(result['profile_path']),groups=groups,
                  batch_size=100,recent_limit=100,wait_seconds=45,interval_seconds=30)
    # Validate before atomically replacing an existing configuration.
    if len({g['chat_id'] for g in groups}) != len(groups) or not 1 <= len(groups) <= 10:
        raise ValueError('invalid_group_selection')
    atomic_json(path, config)
    return dict(status='configured', groups=groups, config=str(path))


def cycle(path, config, reader=native_read):
    started = store.utc_now()
    ids = [g['chat_id'] for g in config['groups']]
    after = store.read_cursor(path, config['account_key'], ids)
    batch = reader(dict(mode='collect',chat_ids=ids,after_id=str(after),
                        batch_size=config['batch_size'],recent_limit=config['recent_limit'],
                        wait_seconds=config['wait_seconds']))
    if batch['status'] != 'ok':
        error = batch.get('error', 'read_failed')
        # Codes only: a malicious/native exception must not become a log message.
        error = error if isinstance(error,str) and re.fullmatch(r'[a-zA-Z_]{1,80}',error) else 'read_failed'
        store.record_failure(path,started,batch['status'],error)
        return dict(status=batch['status'],error=error)
    try:
        return store.save_batch(path,batch,config,after,started)
    except (ValueError,KeyError,TypeError) as error:
        code = str(error) if re.fullmatch(r'[a-z_]{1,80}', str(error)) else 'invalid_batch'
        store.record_failure(path,started,'error',code)
        return dict(status='error',error=code)


def run(path, config, *, once=False, cycles=None):
    store.setup(path)
    with process_lock(Path(path).with_suffix('.viber.lock')):
        log = logging.getLogger('viber_native')
        log.setLevel(logging.INFO)
        log_path = ROOT / 'data' / 'private' / 'viber-collector.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_path,
                                      maxBytes=1024*1024,backupCount=3,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        log.addHandler(handler)
        failures = 0
        try:
            index = 0
            while True:
                try:
                    result = cycle(path,config)
                except sqlite3.Error:
                    result = dict(status='error',error='storage_error')
                text = json.dumps(result,ensure_ascii=True)
                print(text,flush=True)
                log.info(text)
                index += 1
                if result['status']=='error':
                    # Schema, profile or identity problems need investigation.
                    return 1
                if once or (cycles is not None and index >= cycles):
                    return 0 if result['status']=='success' else 2
                failures = min(failures+1,5) if result['status']=='unavailable' else 0
                # Idle is expected while Viber has no committed DB activity.
                delay = min(300, config['interval_seconds'] * 2**failures)
                time.sleep(delay)
        except KeyboardInterrupt:
            log.info('stopped_by_user')
            return 0
        finally:
            log.removeHandler(handler)
            handler.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=CONFIG)
    parser.add_argument('--db',type=Path,default=DEFAULT_PATH)
    commands = parser.add_subparsers(dest='command',required=True)
    commands.add_parser('chats')
    config_parser = commands.add_parser('configure')
    config_parser.add_argument('--group',action='append',required=True)
    run_parser = commands.add_parser('run')
    run_parser.add_argument('--once',action='store_true')
    run_parser.add_argument('--cycles',type=int)
    args = parser.parse_args()
    try:
        if args.command in ('chats','configure'):
            print(json.dumps(configure(args.config,args.group if args.command=='configure' else []),ensure_ascii=True,indent=2))
            return 0
        if args.cycles is not None:
            bounded_int(args.cycles,1,1000000)
        return run(args.db,load_config(args.config),once=args.once,cycles=args.cycles)
    except (ValueError,RuntimeError,OSError,KeyError,TypeError) as error:
        code = str(error) if re.fullmatch(r'[a-z_]{1,80}',str(error)) else type(error).__name__
        print(json.dumps(dict(status='error',error=code)),file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
