from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from viber_native import history, media
from viber_native.store import account_key


class HistoryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]/'.test_tmp'
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.chat = dict(chat_id='101', name='Example group')
        self.profile = 'C:/example/ViberPC/test/viber.db'
        self.cfg = dict(version=1, account_key=account_key(self.profile),
                        groups=[dict(chat_id=101, name='Example group')])
        self.config_path = self.root/'config.json'
        self.config_path.write_text(json.dumps(self.cfg), encoding='utf-8')

    def row(self, event):
        return dict(event_id=str(event), chat_id='101', timestamp='1700000000000',
                    message_type='1', body='Example message')

    def batch(self, rows):
        return dict(status='ok', profile_path=self.profile, chats=[self.chat], rows=rows)

    def test_pagination_keeps_complete_ordered_history(self):
        calls = []
        def reader(request):
            self.assertEqual(request['mode'], 'history')
            calls.append(request)
            return self.batch([self.row(i) for i in range(201, 1, -1)] if len(calls) == 1 else [self.row(1)])
        rows = history.read_group(self.chat, account_key(self.profile), 0, reader=reader)
        self.assertEqual(len(rows), 201)
        self.assertEqual(rows[0]['event_id'], '1')
        self.assertEqual(calls[1]['before_id'], 2)

    def test_changed_profile_or_chat_cannot_export(self):
        for batch in [dict(self.batch([]), profile_path='C:/other/viber.db'),
                      self.batch([dict(self.row(1), chat_id='999')])]:
            with self.assertRaises(ValueError):
                history.read_group(self.chat, account_key(self.profile), 0, reader=lambda _: batch)

    def test_failed_group_does_not_replace_previous_export(self):
        output = self.root/'history.json'
        output.write_text('previous export', encoding='utf-8')
        with patch.object(history, 'read_group', side_effect=RuntimeError('idle')), redirect_stdout(io.StringIO()):
            self.assertEqual(history.main(['--config', str(self.config_path), '--output', str(output)]), 1)
        self.assertEqual(output.read_text(), 'previous export')

    def test_complete_export_is_written(self):
        output = self.root/'history.json'
        with patch.object(history, 'read_group', return_value=[self.row(1)]), redirect_stdout(io.StringIO()):
            self.assertEqual(history.main(['--config', str(self.config_path), '--output', str(output)]), 0)
        self.assertEqual(json.loads(output.read_text())['groups'][0]['rows'][0]['body'], 'Example message')

    def test_download_cli_rejects_unconfigured_chat_before_native_call(self):
        with patch.object(media, 'download_originals', side_effect=AssertionError('unexpected download')), redirect_stdout(io.StringIO()):
            self.assertEqual(media.main(['--config', str(self.config_path), '--chat-id', '999', '--event-id', '1']), 1)


if __name__ == '__main__':
    unittest.main()
