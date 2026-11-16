# -*- coding: utf-8 -*-
"""仓库内置回归测试包。零第三方依赖，直接 python -m unittest discover -s tests。"""

import warnings
# 测试中反复导入/重建模块，SQLite 连接由 GC 回收会触发 ResourceWarning 噪音，
# 与产品行为无关，统一忽略以保持测试输出干净。
warnings.filterwarnings('ignore', category=ResourceWarning)
