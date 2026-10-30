from pathlib import Path
import tempfile
import unittest

from PIL import Image
from viber_native.store import account_key
from viber_native.worker import validate_request
from viber_native.media import download_originals


class NativeDownloadTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.test_tmp'
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.chat = dict(chat_id='103', name='Example group')
        self.profile = account_key('C:/test/viber.db')
        self.row = dict(event_id='44', token='9007199254740999', timestamp='1793261716862',
                        chat_id='103', message_type='2', payload_path='', download_paths='')
        self.image = self.root / 'photo.jpg'
        self.calls = []

    def status(self, **kwargs):
        return dict(status='ok', profile_path='C:/test/viber.db', chats=[self.chat],
                    rows=[dict(self.row, **kwargs)], process_id=123)

    def run_download(self, reader):
        return download_originals(self.chat, self.profile, [44], reader=reader,
                                  roots=[self.root], polls=1, sleeper=lambda _: None)

    def test_success_and_original_verification(self):
        def reader(req):
            self.calls.append(req['mode'])
            if req['mode'] == 'media-request':
                self.assertEqual(req['expected_pid'], 123)
                Image.new('RGB', (50, 60)).save(self.image)
                return dict(status='ok', process_id=123, requested=[44], chat_unchanged=True)
            return self.status(payload_path=str(self.image) if self.image.exists() else '')
        result = self.run_download(reader)
        self.assertTrue(result['complete'])
        self.assertEqual(result['items'][0]['original']['width'], 50)
        self.assertEqual(self.calls, ['media', 'media-request', 'media'])

    def test_existing_photo_does_not_request_again(self):
        Image.new('RGB', (50, 60)).save(self.image)
        def reader(req):
            self.assertEqual(req['mode'], 'media')
            return self.status(payload_path=str(self.image))
        result = self.run_download(reader)
        self.assertTrue(result['complete'])
        self.assertEqual(result['requested'], [])

    def test_wrong_account_chat_or_type_stops_before_request(self):
        for bad in [dict(self.status(), profile_path='C:/other/viber.db'),
                    self.status(chat_id='104'), self.status(message_type='1'),
                    dict(self.status(), rows=[]), dict(self.status(), process_id=None)]:
            def reader(req):
                self.assertEqual(req['mode'], 'media')
                return bad
            with self.assertRaises(ValueError): self.run_download(reader)

    def test_request_ack_does_not_claim_file_downloaded(self):
        def reader(req):
            if req['mode'] == 'media-request':
                return dict(status='ok', process_id=123, requested=[44], chat_unchanged=True)
            return self.status()
        self.assertFalse(self.run_download(reader)['complete'])

    def test_restart_and_event_reuse_cannot_be_accepted(self):
        for change in ['pid', 'token']:
            calls = []
            def reader(req):
                calls.append(req['mode'])
                if req['mode'] == 'media-request':
                    return dict(status='ok', process_id=123, requested=[44], chat_unchanged=True)
                r = self.status()
                if len(calls) > 1:
                    if change == 'pid': r['process_id'] = 124
                    else: r['rows'][0]['token'] = 'different'
                return r
            with self.assertRaises(ValueError): self.run_download(reader)

    def test_bounded_ids_prevent_script_or_sql_injection(self):
        req = dict(mode='media-request', chat_ids=[103], event_ids=[44], expected_pid=123,
                   after_id='0', batch_size=100, recent_limit=0, wait_seconds=5)
        validate_request(req)
        for ids in [['44);sendMessage()'], [True], [44, 44], [9007199254740992], list(range(1, 22))]:
            with self.assertRaises(ValueError): validate_request(dict(req, event_ids=ids))
        with self.assertRaises(ValueError): validate_request(dict(req, expected_pid=None))


if __name__ == '__main__':
    unittest.main()
