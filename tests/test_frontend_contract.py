# -*- coding: utf-8 -*-
"""前端结构契约回归：拦真实使用体验报告指出的可用性回退。

直接读 index.html / static/app.js / static/app.css 源码断言，不需要浏览器，
也不依赖外部冒烟脚本（那些不在仓库里）。
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HTML = (REPO / 'index.html').read_text(encoding='utf-8')
JS = (REPO / 'static' / 'app.js').read_text(encoding='utf-8')
CSS = (REPO / 'static' / 'app.css').read_text(encoding='utf-8')


class TestFirstScreen(unittest.TestCase):
    def test_new_item_before_import(self):
        self.assertLess(HTML.index('id="itemFormCard"'), HTML.index('id="importCard"'),
                        '新增物料应排在批量导入之前')

    def test_import_card_collapsed(self):
        self.assertRegex(HTML, r'<details[^>]*id="importCard"')
        self.assertIn('import-collapse-body', HTML)

    def test_empty_ledger_action_first(self):
        seg = HTML[HTML.index('id="emptyLedgerHint"'):][:400]
        self.assertLess(seg.index('新增物料'), seg.index('回收站'),
                        '空台账提示应先给行动指引，回收站说明放最后')


class TestImportPreview(unittest.TestCase):
    def test_sticky_confirm_bars(self):
        self.assertGreaterEqual(JS.count('import-confirm-bar'), 3,
                                '四类导入确认条都应有吸附容器')
        self.assertRegex(CSS, r'\.import-confirm-bar\{[^}]*position:sticky', re.S)

    def test_confirm_meta_status(self):
        for sel in ('itemConfirmMeta', 'mvConfirmMeta', 'ConfirmMeta'):
            self.assertIn(sel, JS)
        self.assertIn('校验通过', JS)

    def test_mapping_collapsible(self):
        self.assertGreaterEqual(JS.count('class="mapping-details"'), 3)
        self.assertIn('d.can_import&&Object.keys(d.mapping||{}).length', JS,
                      '识别失败时应强制展开字段映射')

    def test_preview_auto_expand(self):
        self.assertIn("$('#importCard').open=true", JS)


class TestMovementForm(unittest.TestCase):
    def test_date_defaults_today(self):
        self.assertRegex(HTML, r'<form id="moveForm"[^>]*novalidate')
        self.assertIn('function todayStr()', JS)
        self.assertIn('setUser(r);initMoveDate()', JS)

    def test_inline_required_hint(self):
        self.assertIn('请填写：', JS)
        self.assertIn('miss.join', JS)


class TestItemTable(unittest.TestCase):
    def test_delete_light_confirm(self):
        seg = JS[JS.index('async function deleteItem'):][:220]
        self.assertIn('uiConfirm', seg, '删除物料应有统一轻确认')

    def test_search_empty_state(self):
        self.assertIn('没有匹配的物料', JS)
        self.assertIn('匹配 0 条', JS)
        self.assertIn('function clearItemFilter()', JS)

    def test_sortable_columns(self):
        self.assertIn("1:'model',8:'available',9:'not_received'", JS)
        self.assertIn("setAttribute('aria-sort'", JS)


class TestRackAndAccessibility(unittest.TestCase):
    def test_empty_components_guidance(self):
        self.assertIn('还没有 46U 部件数据', JS)
        self.assertIn('function goImportServerComponents()', JS)

    def test_labels_bound_to_controls(self):
        labels = re.findall(r'<label[^>]*>', HTML)
        with_for = [l for l in labels if re.search(r'\bfor=', l)]
        self.assertEqual(len(with_for), 31, '31 个显式 label 都应有 for')
        ids = re.findall(r'\bid="([^"]+)"', HTML)
        fors = re.findall(r'<label[^>]*\bfor="([^"]+)"', HTML)
        for f in fors:
            self.assertIn(f, ids, 'for=%s 指向的 id 不存在' % f)
        self.assertEqual(len(ids), len(set(ids)), '不应有重复 id')

    def test_mobile_nav_and_sticky_col(self):
        self.assertRegex(CSS, r'\.tabs\{[^}]*mask-image', re.S)
        self.assertRegex(CSS, r'#items table td:first-child\{[^}]*position:sticky[^}]*left:0', re.S)


if __name__ == '__main__':
    unittest.main()
