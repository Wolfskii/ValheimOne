import http.client
import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

class MapApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ['VALHEIM_SEED_DATA'] = cls.temp.name
        cls.server = importlib.import_module('server')
        cls.worker = importlib.import_module('worker')
        cls.http = cls.server.ThreadingHTTPServer(('127.0.0.1', 0), cls.server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.server.pool.shutdown()
        cls.temp.cleanup()

    def request(self, method, path, data=None, content='application/json'):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        connection.request(method, path, data, {'Content-Type': content})
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def setUp(self):
        self.server.jobs.clear()

    def test_rejects_invalid_inputs_before_generation(self):
        for data in ['{"seed":"../x"}', '{"seed":""}', '{"seed":3}', '{"seed":"ok","path":"x"}', 'null', '["seed"]', '{']:
            self.assertEqual(self.request('POST', '/generate', data)[0], 400)
        self.assertEqual(self.request('POST', '/generate', 'x'*257)[0], 400)
        self.assertEqual(self.request('POST', '/generate', '{}', 'text/plain')[0], 415)
        self.assertEqual(self.request('GET', '/maps/../../engine.log')[0], 404)
        self.assertEqual(self.request('GET', '/status?key=../')[0], 400)

    def test_queue_bound_and_cached_bypass(self):
        key = 'a'*64
        self.server.jobs.update({'x':'running', 'y':'running'})
        with patch.object(self.server, 'key_for', return_value=key), patch.object(self.server.pool, 'submit') as submit:
            self.assertEqual(self.request('POST', '/generate', '{"seed":"Example"}')[0], 429)
            self.assertFalse(submit.called)
            folder = self.worker.ROOT / 'cache' / key
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'result.json').write_text('{}')
            self.assertEqual(self.request('POST', '/generate', '{"seed":"Example"}')[0], 200)
            self.assertFalse(submit.called)

    def test_deduplicates_same_seed(self):
        with patch.object(self.server, 'key_for', return_value='b'*64), patch.object(self.server.pool, 'submit') as submit:
            for _ in range(2):
                self.assertEqual(self.request('POST', '/generate', '{"seed":"Example"}')[0], 202)
            self.assertEqual(submit.call_count, 1)

    def test_preview_does_not_publish_unfinished_locations(self):
        key = 'c'*64
        folder = self.worker.ROOT / 'cache' / key
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'terrain.png').write_bytes(b'png')
        (folder / 'locations.json').write_text('[]')
        self.assertEqual(self.request('GET', f'/maps/{key}/terrain.png')[0], 404)
        (folder / 'preview-result.json').write_text('{"locationsReady":false}')
        self.server.jobs[key] = 'running'
        self.assertEqual(json.loads(self.request('GET', f'/status?key={key}')[1])['state'], 'preview')
        self.assertEqual(self.request('GET', f'/maps/{key}/terrain.png')[0], 200)
        self.assertEqual(self.request('GET', f'/maps/{key}/locations.json')[0], 404)
        self.server.jobs.clear()
        self.assertEqual(json.loads(self.request('GET', f'/status?key={key}')[1])['state'], 'failed')
        (folder / 'result.json').write_text('{}')
        self.assertEqual(self.request('GET', f'/maps/{key}/locations.json')[0], 200)

    def test_unknown_job_is_not_queued_forever(self):
        self.assertEqual(self.request('GET', '/status?key='+'d'*64)[0], 404)

    def test_storage_refuses_without_deleting_current(self):
        folder = self.worker.ROOT / 'cache' / ('e'*64)
        folder.mkdir(parents=True, exist_ok=True)
        marker = folder / 'progress.json'
        marker.write_text('{}')
        with patch.object(self.worker, 'MAX_CACHE_BYTES', 1):
            with self.assertRaises(RuntimeError): self.worker.reserve_cache_space(folder)
        self.assertTrue(marker.exists())

if __name__ == '__main__': unittest.main()
