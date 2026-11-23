# -*- coding: utf-8 -*-
"""API 冒烟：新人克隆后从初始化到登录、增删改查、库存约束的最小闭环。

只操作临时库；验证核心契约（CSRF、正整数库存、型号唯一、回收站）没有被改坏。
"""
import unittest

try:
    from ._base import LiveServer, call
except ImportError:  # python -m unittest discover -s tests 时 _base 是顶层模块
    from _base import LiveServer, call


class TestApiSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = LiveServer().start()
        cls.sid, cls.csrf = cls.srv.admin()
        cls.H = cls.srv.auth_headers(cls.sid, cls.csrf)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_01_status_and_setup_required(self):
        # admin() 已初始化，此时应 setup_required=False
        st, body, _ = call('GET', '/api/status', self.srv.port)
        self.assertEqual(st, 200)
        self.assertFalse(body['setup_required'])

    def test_02_config_public(self):
        st, body, _ = call('GET', '/api/config', self.srv.port)
        self.assertEqual(st, 200)
        self.assertEqual(body['qty_max'], 10000000)
        self.assertEqual(body['rack_u_slots'], 46)
        self.assertEqual(body['item_categories'], ['光模块', '线缆'])
        self.assertFalse(body['configurable'])

    def test_03_me_and_csrf(self):
        st, me, _ = call('GET', '/api/me', self.srv.port, headers=self.H)
        self.assertEqual(st, 200)
        self.assertTrue(me['is_admin'])
        # 无 CSRF 的写请求必须被拒
        st, _, _ = call('POST', '/api/items', self.srv.port,
                        body={'model': 'X'}, headers={'Cookie': 'sid=' + self.sid})
        self.assertEqual(st, 403)

    def test_04_item_crud_and_unique(self):
        payload = {'category': '光模块', 'model': 'TEST-API-1', 'description': '冒烟物料', 'planned': 5}
        st, _, _ = call('POST', '/api/items', self.srv.port, body=payload, headers=self.H)
        self.assertEqual(st, 201)
        # 型号唯一（不区分大小写）
        st, body, _ = call('POST', '/api/items', self.srv.port,
                           body=dict(payload, model='test-api-1'), headers=self.H)
        self.assertEqual(st, 409)
        st, body, _ = call('GET', '/api/items', self.srv.port, headers=self.H)
        self.assertEqual(st, 200)
        items = body['items'] if isinstance(body, dict) else body
        self.assertTrue(any(i['model'] == 'TEST-API-1' for i in items))

    def test_05_movement_integer_and_no_negative(self):
        base = {'model': 'TEST-API-1', 'kind': '入库', 'category': '光模块',
                'moved_at': '2026-11-16'}
        # 小数、负数、零都必须被拒
        for bad in (1.5, -3, 0):
            st, _, _ = call('POST', '/api/movements', self.srv.port,
                            body=dict(base, qty=bad), headers=self.H)
            self.assertEqual(st, 400, '数量 %r 应被拒绝' % bad)
        # 正常入库
        st, _, _ = call('POST', '/api/movements', self.srv.port,
                        body=dict(base, qty=10), headers=self.H)
        self.assertEqual(st, 201)
        # 超额领用导致负库存必须被拒
        st, _, _ = call('POST', '/api/movements', self.srv.port,
                        body=dict(base, kind='领用/安装', qty=9999), headers=self.H)
        self.assertEqual(st, 409)

    def test_05a_sn_on_movements(self):
        """条目级 SN：入库选填、报废必填且逐件回填坏件、重复 SN 被拒。"""
        payload = {'category': '光模块', 'model': 'TEST-SN-1', 'description': 'SN 测试物料'}
        st, _, _ = call('POST', '/api/items', self.srv.port, body=payload, headers=self.H)
        self.assertIn(st, (201, 409))  # 重跑时可能已存在
        base = {'model': 'TEST-SN-1', 'category': '光模块', 'moved_at': '2026-11-16'}
        # 入库：SN 选填且保存
        st, _, _ = call('POST', '/api/movements', self.srv.port,
                        body=dict(base, kind='入库', qty=2, sn='IN-SN-1, IN-SN-2'), headers=self.H)
        self.assertEqual(st, 201)
        st, body, _ = call('GET', '/api/movements', self.srv.port, headers=self.H)
        found = [m for m in body['movements'] if m['model'] == 'TEST-SN-1' and m['kind'] == '入库']
        self.assertTrue(found and found[0]['sn'] == 'IN-SN-1, IN-SN-2', found)
        # 报废：缺 SN 必须被拒
        st, resp, _ = call('POST', '/api/movements', self.srv.port,
                           body=dict(base, kind='报废', qty=2), headers=self.H)
        self.assertEqual(st, 400)
        self.assertIn('SN', str(resp))
        # 报废：SN 数量与 qty 不符被拒
        st, _, _ = call('POST', '/api/movements', self.srv.port,
                        body=dict(base, kind='报废', qty=2, sn='ONLY-ONE'), headers=self.H)
        self.assertEqual(st, 400)
        # 报废：SN 正确，逐件回填坏件
        st, _, _ = call('POST', '/api/movements', self.srv.port,
                        body=dict(base, kind='报废', qty=2, sn='BAD-SN-1, BAD-SN-2'), headers=self.H)
        self.assertEqual(st, 201)
        import sqlite3
        with sqlite3.connect(self.srv.db_path) as c:
            serials = [r[0] for r in c.execute(
                "SELECT serial_number FROM material_spares WHERE serial_number LIKE 'BAD-SN-%' ORDER BY serial_number")]
        self.assertEqual(serials, ['BAD-SN-1', 'BAD-SN-2'])
        # 重复 SN 被拒
        st, _, _ = call('POST', '/api/movements', self.srv.port,
                        body=dict(base, kind='入库', qty=1, sn='BAD-SN-1'), headers=self.H)
        self.assertEqual(st, 409)

    def test_06_export_xlsx(self):
        """曾有回归：拆分漏 import xml_escape 导致导出 500。"""
        st, body, _ = call('GET', '/api/export/items.xlsx', self.srv.port, headers=self.H)
        self.assertEqual(st, 200)
        self.assertTrue(body[:2] == b'PK', '导出应是 xlsx（zip）二进制')

    def test_07_delete_to_recycle(self):
        st, items, _ = call('GET', '/api/items', self.srv.port, headers=self.H)
        rows = items['items'] if isinstance(items, dict) else items
        row = next(r for r in rows if r['model'] == 'TEST-API-1')
        # 有关联明细的物料要求输入完整型号二次确认
        st, _, _ = call('DELETE', '/api/items/%d' % row['id'], self.srv.port, headers=self.H)
        self.assertEqual(st, 409)
        st, _, _ = call('DELETE', '/api/items/%d' % row['id'], self.srv.port,
                        body={'confirm_model': 'TEST-API-1'}, headers=self.H)
        self.assertEqual(st, 200)
        st, body, _ = call('GET', '/api/recycle', self.srv.port, headers=self.H)
        self.assertEqual(st, 200)

    def test_08_logout(self):
        st, _, _ = call('POST', '/api/logout', self.srv.port, headers=self.H)
        self.assertEqual(st, 200)
        st, _, _ = call('GET', '/api/me', self.srv.port, headers=self.H)
        self.assertEqual(st, 401)


if __name__ == '__main__':
    unittest.main()
