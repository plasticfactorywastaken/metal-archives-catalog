import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_mirror.cli import main
from catalog_mirror.store import Store


class CLITests(unittest.TestCase):
    def test_sheets_only_failure_is_observable_and_never_requests_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); config=root/'settings.json'
            config.write_text(json.dumps({'database':str(root/'catalog.sqlite'),
                'export_directory':str(root/'data'),'sheet_id':'test-sheet'}))
            with patch('catalog_mirror.cli.Transport') as transport, patch.dict('os.environ',
                    {'GOOGLE_SERVICE_ACCOUNT_JSON':'','GOOGLE_SHEET_ID':''}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--config',str(config),'sync-sheets']),1)
                transport.assert_not_called()
            with Store(root/'catalog.sqlite') as store:
                self.assertEqual(store.runs(1)[0]['sheets_status'],'failed')

    def test_init_export_and_summary_without_source_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            config=root/'settings.json'
            config.write_text(json.dumps({'database':str(root/'catalog.sqlite'),'export_directory':str(root/'data')}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--config',str(config),'init']),0)
                self.assertEqual(main(['--config',str(config),'export']),0)
                self.assertEqual(main(['--config',str(config),'summary']),0)
            manifest=json.loads((root/'data'/'manifest.json').read_text())
            self.assertEqual(manifest['total_records'],0)
            summary=json.loads((root/'data'/manifest['summary']).read_text())
            self.assertIsNone(summary['estimated_coverage'])

    def test_missing_sheets_credentials_records_partial_without_erasing_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); config=root/'settings.json'
            settings={'database':str(root/'catalog.sqlite'),'export_directory':str(root/'data'),
                'user_agent':'CatalogMirrorTest/1','api_base_url':'https://metal-api.dev',
                'website_base_url':'https://www.metal-archives.com','sheet_id':'test-sheet'}
            config.write_text(json.dumps(settings))
            with patch('catalog_mirror.cli.ApiSource') as api,patch('catalog_mirror.cli.WebsiteSource') as website,patch('catalog_mirror.cli.Transport'),patch.dict('os.environ',{'GOOGLE_SERVICE_ACCOUNT_JSON':'','GOOGLE_SHEET_ID':''}):
                api.return_value.probe.return_value={'status':'unavailable'}
                website.return_value.probe.return_value={'status':'blocked'}
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['--config',str(config),'sync','--mode','test','--limit','1']),0)
            with Store(root/'catalog.sqlite') as store:
                run=store.runs(1)[0]
                self.assertEqual(run['status'],'partial')
                self.assertEqual(run['sheets_status'],'failed')
                self.assertEqual(store.summary()['total_records'],0)


if __name__=='__main__': unittest.main()
