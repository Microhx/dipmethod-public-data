import csv
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dipmethod_public_data.archive import ArchiveError
from dipmethod_public_data.publish import main, publication, verify_github

COMMIT = 'f' * 40


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.day = self.root / 'data/2026-09-30'
        self.stock = self.day / '300968.SZ'
        self.stock.mkdir(parents=True)
        with (self.day / 'signals.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['id', 'sig_date', 'code', 'name', 'cost'])
            writer.writeheader()
            writer.writerow(dict(id='sig-1', sig_date='2026-09-30', code='300968.SZ', name='格林精密', cost='11.55'))
            writer.writerow(dict(id='sig-2', sig_date='2026-09-30', code='', name='无代码', cost='1'))
        days = []
        for date in ['2026-09-30', '2026-10-09']:
            content = b't,c\n09:31,11.55\n'
            (self.stock / f'{date}.csv').write_bytes(content)
            (self.stock / f'{date}.png').write_bytes(b'chart' + date.encode())
            days.append(dict(date=date, status='partial', rows=1, missing_labels=['09:32'], sha256=hashlib.sha256(content).hexdigest()))
        self.metadata = dict(code='300968.SZ', signal_date='2026-09-30', names=['格林精密'], signal_ids=['sig-1'], buy_points=[11.55], next_trade_date='2026-10-09', days=days)
        self.save_metadata()

    def save_metadata(self):
        (self.stock / 'metadata.json').write_text(json.dumps(self.metadata))

    def test_content_addresses_and_exact_next_trade_date(self):
        objects, indexes = publication(self.root, github_commit=COMMIT)
        self.assertEqual(len(objects), 2)
        self.assertTrue(all(key.endswith('.png') for key in objects))
        records = indexes['2026-09-30']['signals']
        self.assertEqual(records[0]['next_trade_date'], '2026-10-09')
        self.assertEqual(records[1]['days'], [])
        self.assertIn('intraday/2026-09-30/300968.SZ/2026-10-09-', records[0]['days'][1]['png'])
        self.assertEqual(records[0]['cost'], '11.55')
        self.assertEqual(records[0]['days'][0]['missing_labels'], ['09:32'])
        self.assertEqual(records[0]['days'][0]['csv'], {
            'repo': 'Microhx/dipmethod-public-data', 'commit': COMMIT,
            'path': 'data/2026-09-30/300968.SZ/2026-09-30.csv',
            'sha256': self.metadata['days'][0]['sha256'],
        })
        before = records[0]['days'][0]['png']
        (self.stock / '2026-09-30.png').write_bytes(b'new chart')
        self.assertNotEqual(before, publication(self.root, github_commit=COMMIT)[1]['2026-09-30']['signals'][0]['days'][0]['png'])

    def test_reject_inconsistent_archive_identity_and_corrupt_csv(self):
        self.metadata['buy_points'] = [12]
        self.save_metadata()
        with self.assertRaises(ArchiveError):
            publication(self.root, github_commit=COMMIT)
        self.metadata['buy_points'] = [11.55]
        self.save_metadata()
        (self.stock / '2026-09-30.csv').write_bytes(b'changed')
        with self.assertRaises(ArchiveError):
            publication(self.root, github_commit=COMMIT)

    def github_tree(self):
        return {
            path.relative_to(self.root).as_posix():
            hashlib.sha1(b'blob ' + str(len(path.read_bytes())).encode() + b'\0' + path.read_bytes()).hexdigest()
            for path in self.stock.glob('*.csv')
        }

    def test_verify_public_commit_rejects_missing_or_different_csv(self):
        _, indexes = publication(self.root, github_commit=COMMIT)
        tree = self.github_tree()
        with patch('dipmethod_public_data.publish.github_blobs', return_value=tree):
            self.assertEqual(verify_github(self.root, indexes, COMMIT), 2)
        tree[next(iter(tree))] = '0' * 40
        with patch('dipmethod_public_data.publish.github_blobs', return_value=tree):
            with self.assertRaises(ArchiveError):
                verify_github(self.root, indexes, COMMIT)
        tree = {}
        with patch('dipmethod_public_data.publish.github_blobs', return_value=tree):
            with self.assertRaises(ArchiveError):
                verify_github(self.root, indexes, COMMIT)

    def run_publisher(self, corrupt=False, unpublished=False):
        stored = {}
        verified = set()

        def transfer(url, *, content=None, headers=None):
            key = url.split('/objects/', 1)[1] if '/objects/' in url else url.split('cdn.dipmethod.com/', 1)[1]
            if content is not None:
                if '/manifests/' in key:
                    self.assertEqual(len(verified), 2, 'Never publish an index before all public files verify')
                self.assertFalse(key.endswith('.csv'), 'CSV must never be uploaded to R2')
                stored[key] = content
                return b''
            if 'cdn.dipmethod.com/' in url:
                verified.add(key)
                return b'corrupt' if corrupt else stored[key]
            return stored[key]

        args = ['publish', '--account-id', 'a' * 32, '--upload', '--jobs', '2']
        with patch('dipmethod_public_data.publish.ROOT', self.root), patch('sys.argv', args), \
                patch('dipmethod_public_data.publish.github_commit', return_value=COMMIT), \
                patch('dipmethod_public_data.publish.github_blobs', return_value={} if unpublished else self.github_tree()), \
                patch('dipmethod_public_data.publish.credentials', return_value='test-credential'), \
                patch('dipmethod_public_data.publish.transfer', side_effect=transfer), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = main()
        return result, stored

    def test_index_published_only_after_all_public_files_verify(self):
        result, stored = self.run_publisher()
        self.assertEqual(result, 0)
        self.assertIn('intraday/manifests/2026-09-30.json', stored)

    def test_bad_public_file_prevents_index_publication(self):
        result, stored = self.run_publisher(corrupt=True)
        self.assertEqual(result, 1)
        self.assertFalse(any('/manifests/' in key for key in stored))

    def test_unpublished_csv_prevents_all_r2_writes(self):
        result, stored = self.run_publisher(unpublished=True)
        self.assertEqual(result, 1)
        self.assertEqual(stored, {})


if __name__ == '__main__':
    unittest.main()
