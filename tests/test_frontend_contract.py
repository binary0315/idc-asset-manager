# -*- coding: utf-8 -*-
"""前端结构契约回归：拦真实使用体验报告指出的可用性回退。

直接读 index.html / static/app.js / static/app.css 源码断言，不需要浏览器，
也不依赖外部冒烟脚本（那些不在仓库里）。
"""
import re
import shutil
import subprocess
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
    @unittest.skipUnless(shutil.which('node'), '未安装 Node，跳过 setUser 运行时校验')
    def test_date_defaults_today(self):
        # 不 grep 失效路径：真实执行 setUser()，覆盖 setup/login/会话恢复三条路径
        self.assertRegex(HTML, r'<form id="moveForm"[^>]*novalidate')
        r = subprocess.run(
            ['node', str(REPO / 'tests' / '_verify_setuser.js'), str(REPO / 'static' / 'app.js')],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('三条路径均默认今天', r.stdout)

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
        # 表头用 data-sort-key 声明，JS 按属性查找，不再硬编码列序号
        self.assertIn('data-sort-key="model"', HTML)
        self.assertIn('data-sort-key="available"', HTML)
        self.assertIn('data-sort-key="not_received"', HTML)
        self.assertIn("querySelectorAll('#items thead th[data-sort-key]')", JS)
        self.assertIn("setAttribute('aria-sort'", JS)
        # colspan 常量必须与表头实际列数一致，否则加列时测试应红
        m = re.search(r'const ITEM_COL_COUNT=(\d+)', JS)
        self.assertTrue(m, '应定义 ITEM_COL_COUNT 常量')
        # 精确数台账表头列数：从 <table> 到 itemsBody 之间的 <th>
        seg = HTML[HTML.rfind('<table', 0, HTML.index('id="itemsBody"')):HTML.index('id="itemsBody"')]
        th_count = len(re.findall(r'<th[ >]', seg))
        self.assertEqual(int(m.group(1)), th_count,
                         'ITEM_COL_COUNT 与台账表头列数不一致（加列后需同步）')


class TestRackAndAccessibility(unittest.TestCase):
    def test_empty_components_guidance(self):
        self.assertIn('component-empty-notice', JS)  # 只断言结构，文案可精简
        self.assertIn('function goImportServerComponents()', JS)

    def test_labels_bound_to_controls(self):
        ids = re.findall(r'\bid="([^"]+)"', HTML)
        self.assertEqual(len(ids), len(set(ids)), '不应有重复 id')
        # 每个 label 要么 for 指向存在的 id，要么自身包裹控件（隐式关联）
        for m in re.finditer(r'<label([^>]*)>(.*?)</label>', HTML, re.S):
            attrs, inner = m.group(1), m.group(2)
            fm = re.search(r'\bfor="([^"]+)"', attrs)
            if fm:
                self.assertIn(fm.group(1), ids, 'for=%s 指向的 id 不存在' % fm.group(1))
            else:
                self.assertTrue(re.search(r'<(input|select|textarea)\b', inner),
                                '无 for 的 label 必须包裹控件：' + m.group(0)[:80])

    def test_mobile_nav_and_sticky_col(self):
        self.assertRegex(CSS, r'\.tabs\{[^}]*mask-image', re.S)
        self.assertRegex(CSS, r'#items table td:first-child\{[^}]*position:sticky[^}]*left:0', re.S)


class TestRackGrid(unittest.TestCase):
    def test_rack_row_uses_responsive_wrap(self):
        """机柜格子响应式换行，不再用固定 10 列把几十个机柜铺成一行。"""
        self.assertRegex(CSS, r'\.rack-cells\{[^}]*repeat\(auto-fill', re.S)
        self.assertNotIn('grid-template-columns:repeat(10', CSS)
        self.assertNotIn('grid-auto-flow:column', CSS)
        self.assertNotIn('min-width:810px', CSS)

    def test_rack_rows_use_per_row_position(self):
        """每排按自己的最大机柜号生成格子，不拿全局最大号给所有排铺满。"""
        # 从每排生成格子的代码段断言（rowRacks 是本次修复引入的唯一标识）
        idx = JS.index('let rowRacks=roomRacks.filter')
        seg = JS[idx:][:600]
        self.assertIn('let rowRacks=roomRacks.filter', seg, '应按排筛选机柜')
        self.assertIn('Math.min(Math.max(...nums,12)+2,60)', seg, '每排上限应收敛')
        self.assertNotIn('maxPosition=Math.max', seg, '不应再用全局最大机柜号')


if __name__ == '__main__':
    unittest.main()
