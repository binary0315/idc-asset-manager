# -*- coding: utf-8 -*-
"""测试公共基础设施：临时数据库 + 独立端口的 HTTP 服务，不碰真实数据。

关键：必须在 import app 之前设置 IDC_DB / IDC_PORT，因为 config 在导入时
读取这些环境变量。每个测试类通过 start_server() 拿到 (base_url, db_path)。
"""
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def call(method, path, port, body=None, headers=None, timeout=15):
    """发一个 HTTP 请求，返回 (status, parsed_body, headers_dict)。"""
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        h.setdefault('Content-Type', 'application/json')
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        ctype = resp.getheader('Content-Type', '')
        if 'json' in ctype and raw:
            try:
                parsed = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                parsed = raw
        else:
            parsed = raw
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, parsed, hdrs
    finally:
        conn.close()


def wait_ready(port, path='/api/status', tries=60):
    for _ in range(tries):
        try:
            urllib.request.urlopen('http://127.0.0.1:%d%s' % (port, path), timeout=1).read()
            return True
        except Exception:
            time.sleep(0.1)
    return False


class LiveServer(object):
    """在独立线程里起一个 ThreadingHTTPServer 实例，项目根目录作为 cwd。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix='idc_test_')
        self.db_path = os.path.join(self.tmp, 'nested', 'isolated.sqlite3')  # 故意用多级目录验证自动建父目录
        self.port = self._free_port()

    @staticmethod
    def _free_port():
        import socket
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def start(self):
        os.environ['IDC_DB'] = self.db_path
        os.environ['IDC_PORT'] = str(self.port)
        os.environ['IDC_HOST'] = '127.0.0.1'
        os.chdir(str(REPO))
        # 清掉可能已缓存的 app 模块，确保用新的环境变量重新导入
        import sys
        for name in list(sys.modules):
            if name == 'app' or name.startswith('idc.'):
                del sys.modules[name]
        import app
        self.app = app
        app.init()
        from http.server import ThreadingHTTPServer
        self.httpd = ThreadingHTTPServer(('127.0.0.1', self.port), app.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        if not wait_ready(self.port):
            raise RuntimeError('测试服务未就绪')
        return self

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass

    def admin(self):
        """执行初始化+登录，返回 (sid, csrf)。"""
        status, body, hdrs = call('POST', '/api/setup', self.port,
                                  {'username': 'testadmin', 'password': 'test-password-123'})
        assert status == 200, (status, body)
        csrf = body['csrf']
        cookie = hdrs['set-cookie'].split(';')[0]
        sid = cookie.split('=', 1)[1]
        return sid, csrf

    def auth_headers(self, sid, csrf):
        return {'Cookie': 'sid=' + sid, 'X-CSRF-Token': csrf}
