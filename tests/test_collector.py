from contextlib import closing
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from viber_native import store as store
from viber_native.collector import cycle, native_read, load_config, atomic_json, process_lock
from viber_native.worker import validate_request, main as worker_main


class ViberCollectorTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.test_tmp'
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'viber.sqlite'
        store.setup(self.path)
        self.profile = 'C:/test/ViberPC/account/viber.db'
        self.config = dict(version=1,account_key=store.account_key(self.profile),
                           groups=[dict(chat_id=101,name='Group A'),dict(chat_id=102,name='Group B')],
                           batch_size=2,recent_limit=2,wait_seconds=1,interval_seconds=5)

    def row(self, event=1, chat=101, body='Example message', token=None):
        return dict(event_id=str(event),chat_id=str(chat),token=token or str(9000000000000000000+event),
                    timestamp='1793358000000',direction='0',message_type='0',body=body,status='0')

    def batch(self, rows, recent=None, upper=10):
        return dict(status='ok',profile_path=self.profile,chats=[dict(chat_id='101',name='Group A'),
                    dict(chat_id='102',name='Group B')],rows=rows,recent=recent or [],upper_id=str(upper))

    def query(self, sql):
        with closing(sqlite3.connect(self.path)) as con:
            return con.execute(sql).fetchall()

    def test_replay_dedup_edit_history_and_large_token(self):
        batch = self.batch([self.row()],recent=[self.row()])
        first = store.save_batch(self.path,batch,self.config,0,'start')
        self.assertEqual(first['inserted'],1)
        self.assertEqual(first['cursor'],10)
        replay = store.save_batch(self.path,self.batch([],recent=[self.row()]),self.config,10,'start')
        self.assertEqual(replay['inserted'],0)
        edited = store.save_batch(self.path,self.batch([],recent=[self.row(body='new rate')]),self.config,10,'start')
        self.assertEqual(edited['updated'],1)
        self.assertEqual(self.query('SELECT token,revision FROM viber_messages'),[('9000000000000000001',2)])
        self.assertEqual(len(self.query('SELECT * FROM viber_message_revisions')),2)

    def test_cursor_pages_restart_and_zero_new_rows(self):
        first = store.save_batch(self.path,self.batch([self.row(1),self.row(2,102)]),self.config,0,'start')
        self.assertEqual(first['cursor'],2)
        store.setup(self.path)
        self.assertEqual(store.read_cursor(self.path,self.config['account_key'],[101,102]),2)
        store.save_batch(self.path,self.batch([]),self.config,2,'start')
        self.assertEqual(store.read_cursor(self.path,self.config['account_key'],[101,102]),10)

    def test_wrong_group_profile_or_order_cannot_advance(self):
        bad_batches=[self.batch([self.row(chat=999)]),self.batch([self.row(2),self.row(1)])]
        changed=self.batch([self.row()]);changed['profile_path']='C:/other/viber.db';bad_batches.append(changed)
        renamed=self.batch([self.row()]);renamed['chats'][0]['name']='Other';bad_batches.append(renamed)
        for batch in bad_batches:
            with self.assertRaises(ValueError):store.save_batch(self.path,batch,self.config,0,'start')
        self.assertEqual(self.query('SELECT * FROM viber_messages'),[])
        self.assertEqual(self.query('SELECT * FROM viber_chats'),[])

    def test_collision_rolls_back_entire_batch(self):
        store.save_batch(self.path,self.batch([self.row(3)]),self.config,0,'start')
        batch=self.batch([self.row(1),self.row(2)],recent=[self.row(3,token='different')])
        # Numeric but changed token emulates an event ID reused after a DB reset.
        batch['recent'][0]['token']='123'
        with self.assertRaisesRegex(ValueError,'event_id_reused'):
            store.save_batch(self.path,batch,self.config,0,'start')
        self.assertEqual(self.query('SELECT event_id FROM viber_messages'),[(3,)])
        self.assertEqual(self.query('SELECT count(*) FROM viber_runs'),[(1,)])

    def test_idle_disconnect_and_recovery_preserve_cursor(self):
        for status in ['idle','unavailable']:
            result=cycle(self.path,self.config,reader=lambda _:dict(status=status,error='waiting'))
            self.assertEqual(result['status'],status)
            self.assertEqual(store.read_cursor(self.path,self.config['account_key'],[101,102]),0)
        result=cycle(self.path,self.config,reader=lambda _:self.batch([self.row()]))
        self.assertEqual(result['inserted'],1)

    def test_bad_later_row_is_rejected_before_any_writes(self):
        bad=self.row(2);bad['body']=None
        with self.assertRaises(ValueError):
            store.save_batch(self.path,self.batch([self.row(),bad]),self.config,0,'start')
        self.assertEqual(self.query('SELECT count(*) FROM viber_messages'),[(0,)])

    def test_database_reset_is_detected(self):
        with self.assertRaisesRegex(ValueError,'source_database_reset'):
            store.save_batch(self.path,self.batch([],upper=3),self.config,10,'start')

    def test_event_cannot_silently_move_between_selected_chats(self):
        store.save_batch(self.path,self.batch([self.row()]),self.config,0,'start')
        with self.assertRaisesRegex(ValueError,'event_id_reused'):
            store.save_batch(self.path,self.batch([],recent=[self.row(chat=102)]),self.config,10,'start')
        self.assertEqual(self.query('SELECT chat_id FROM viber_messages'),[(101,)])

    def test_worker_deadline_and_broken_payload(self):
        request=dict(wait_seconds=1)
        with patch('viber_native.collector.subprocess.run',side_effect=subprocess.TimeoutExpired('worker',21)):
            self.assertEqual(native_read(request)['error'],'worker_timeout')
        with patch('viber_native.collector.subprocess.run',return_value=subprocess.CompletedProcess([],0,'garbage','')):
            self.assertEqual(native_read(request)['error'],'invalid_worker_response')

    def test_config_and_sql_injection_bounds(self):
        path=Path(self.tmp.name)/'config.json';atomic_json(path,self.config)
        self.assertEqual(load_config(path)['groups'],self.config['groups'])
        self.config['groups'][0]['chat_id']='101); DELETE FROM Messages; --'
        atomic_json(path,self.config)
        with self.assertRaises(ValueError):load_config(path)
        with self.assertRaises(ValueError):
            validate_request(dict(mode='collect',wait_seconds=1,batch_size=2,chat_ids=['1; DROP TABLE Messages'],after_id='0'))

    def test_second_collector_is_blocked(self):
        lock=Path(self.tmp.name)/'test.lock'
        with process_lock(lock):
            with self.assertRaisesRegex(RuntimeError,'collector_already_running'):
                with process_lock(lock):pass
        with process_lock(lock):pass

    def test_restart_race_is_retryable_and_exception_details_are_redacted(self):
        error_type=type('ProcessNotFoundError',(Exception,),{})
        output=io.StringIO()
        with (patch('sys.stdin',io.StringIO('{}')),
              patch('viber_native.worker.read_native',side_effect=error_type('private text')),
              redirect_stdout(output)):
            worker_main()
        result=json.loads(output.getvalue())
        self.assertEqual(result['status'],'unavailable')
        self.assertNotIn('private text',output.getvalue())


if __name__=='__main__':
    unittest.main()
