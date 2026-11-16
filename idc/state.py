# -*- coding: utf-8 -*-
"""state.py：按职责拆分，显式依赖，无自导入/循环。"""

import threading
IMPORT_TOKENS = {}
MOVEMENT_IMPORT_TOKENS = {}
SERVER_IMPORT_TOKENS = {}
SERVER_COMPONENT_IMPORT_TOKENS = {}
LOGIN_FAILS = {}          # (ip, username) -> [失败次数, 首次失败时间, 解锁时间]
LOGIN_LOCK = threading.Lock()
IDEMPOTENCY_LOCK = threading.Lock()
