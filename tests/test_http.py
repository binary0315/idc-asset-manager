# -*- coding: utf-8 -*-
"""HTTP 契约回归：静态缓存、HEAD、304/204 的 Content-Length、路径穿越。

这些是最近几轮反复出问题的点（304/204 曾错误携带 Content-Length），必须有测试拦着。
"""
import unittest

try:
    from ._base import LiveServer, call
except ImportError:  # python -m unittest discover -s tests 时 _base 是顶层模块
    from _base import LiveServer, call


class TestStaticAndHeaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = LiveServer().start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_root_injects_version(self):
        st, body, hdrs = call('GET', '/', self.srv.port)
        self.assertEqual(st, 200)
        self.assertIn(b'/static/app.css?v=', body)
        self.assertIn(b'/static/app.js?v=', body)

    def test_static_etag_and_cache(self):
        st, css, h = call('GET', '/static/app.css', self.srv.port)
        self.assertEqual(st, 200)
        self.assertTrue(len(css) > 100)
        self.assertTrue(h.get('etag', '').startswith('"'))
        self.assertIn('max-age=3600', h.get('cache-control', ''))
        self.assertEqual(h.get('x-content-type-options'), 'nosniff')

    def test_304_has_no_content_length(self):
        st, css, h = call('GET', '/static/app.css', self.srv.port)
        etag = h['etag']
        st, body, h2 = call('GET', '/static/app.css', self.srv.port,
                            headers={'If-None-Match': etag})
        self.assertEqual(st, 304)
        self.assertEqual(body, b'')
        self.assertNotIn('content-length', h2, '304 不得携带 Content-Length')
        self.assertEqual(h2.get('etag'), etag)

    def test_if_none_match_star(self):
        st, _, _ = call('GET', '/static/app.css', self.srv.port,
                        headers={'If-None-Match': '*'})
        self.assertEqual(st, 304)

    def test_head_no_body_same_headers(self):
        st, body, h = call('GET', '/static/app.css', self.srv.port)
        st2, body2, h2 = call('HEAD', '/static/app.css', self.srv.port)
        self.assertEqual(st2, 200)
        self.assertEqual(body2, b'')
        self.assertEqual(h2.get('content-length'), h.get('content-length'))
        self.assertEqual(h2.get('etag'), h.get('etag'))

    def test_options_204_no_content_length(self):
        st, body, h = call('OPTIONS', '/api/status', self.srv.port)
        self.assertEqual(st, 204)
        self.assertEqual(body, b'')
        self.assertNotIn('content-length', h, '204 不得携带 Content-Length')

    def test_static_path_traversal_blocked(self):
        for bad in ('/static/app.py', '/static/../app.py', '/static/%2e%2e/app.py'):
            st, _, _ = call('GET', bad, self.srv.port)
            self.assertIn(st, (400, 404), bad)

    def test_api_no_store(self):
        st, _, h = call('GET', '/api/config', self.srv.port)
        self.assertEqual(st, 200)
        self.assertEqual(h.get('cache-control'), 'no-store')


if __name__ == '__main__':
    unittest.main()
