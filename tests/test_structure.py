# -*- coding: utf-8 -*-
"""结构回归：模块导入顺序、无自导入/循环、公开符号契约。

这几条专门拦拆分后最容易回退的问题（曾因清理通配导入漏掉 xml_escape 导致导出 500）。
用干净子进程 import，模拟新人克隆后第一次运行的真实路径。
"""
import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IDC_DIR = REPO / 'idc'


class TestImportOrder(unittest.TestCase):
    def test_app_imports_clean_in_subprocess(self):
        """干净进程里 import app 必须成功，且无需预置任何服务器状态。"""
        code = (
            "import sys; sys.path.insert(0, %r); "
            "import app; "
            "assert callable(app.db); "
            "assert callable(app.xlsx_matrix); "
            "assert callable(app.make_xlsx); "
            "assert callable(app.pw_hash); "
            "print('OK')" % str(REPO)
        )
        r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                           cwd=str(REPO), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertIn('OK', r.stdout)

    def test_all_idc_modules_import(self):
        code = (
            "import sys, importlib; sys.path.insert(0, %r); "
            "[importlib.import_module('idc.' + m) for m in "
            "('config','state','db','crypto','validation','excel','idem','stock','audit')]; "
            "print('OK')" % str(REPO)
        )
        r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                           cwd=str(REPO), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertIn('OK', r.stdout)

    def test_no_self_import_or_wildcard_from_sibling(self):
        """模块不得 from .config import * / from .state import *（曾导致符号被静默覆盖）。"""
        for f in IDC_DIR.glob('*.py'):
            tree = ast.parse(f.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    for alias in node.names:
                        self.assertNotEqual(alias.name, '*', '%s 存在兄弟模块通配导入' % f.name)


class TestPublicContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(REPO))
        os.chdir(str(REPO))
        import app
        cls.app = app

    def test_db_re_export_removed(self):
        """app.DB / config.DB 已移除——旧脚本必须改用 IDC_DB，不再静默无效。"""
        self.assertFalse(hasattr(self.app, 'DB'), 'app.DB 死导入不应存在')
        import idc.config as cfg
        self.assertFalse(hasattr(cfg, 'DB'), 'config.DB 死变量不应存在')

    def test_get_db_path_follows_env(self):
        import idc.config as cfg
        old = os.environ.get('IDC_DB')
        try:
            os.environ['IDC_DB'] = 'C:/tmp/custom/path.sqlite3'
            self.assertEqual(cfg.get_db_path(), Path('C:/tmp/custom/path.sqlite3'))
        finally:
            if old is None:
                os.environ.pop('IDC_DB', None)
            else:
                os.environ['IDC_DB'] = old

    def test_config_endpoint_keys(self):
        """配置接口的口径键是前端契约，改名会破坏前端。"""
        expected = {'item_categories', 'item_fields', 'field_limits',
                    'material_spare_statuses', 'server_spare_statuses',
                    'rack_u_slots', 'qty_max', 'import', 'idempotency', 'configurable'}
        # 从源码静态确认这些键存在，避免起服务
        src = (REPO / 'app.py').read_text(encoding='utf-8')
        for key in expected:
            self.assertIn("'%s'" % key, src, '配置接口缺少键 %s' % key)


if __name__ == '__main__':
    unittest.main()
