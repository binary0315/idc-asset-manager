# -*- coding: utf-8 -*-
"""config.py：按职责拆分，显式依赖，无自导入/循环。"""

from pathlib import Path
import os
ROOT = Path(__file__).resolve().parent.parent
def get_db_path():
    """统一数据库路径来源：优先 IDC_DB 环境变量，便于测试隔离。"""
    return Path(os.environ.get('IDC_DB') or (ROOT / 'idc_inventory.sqlite3'))


DB = get_db_path()
HOST = os.environ.get('IDC_HOST', '127.0.0.1')
PORT = int(os.environ.get('IDC_PORT', '8002'))
SESSION_SECONDS = 12 * 60 * 60
IDEMPOTENCY_AUTO_BUCKET = 3
IDEMPOTENT_POST = {'/api/items', '/api/movements', '/api/servers', '/api/users'}
IDEMPOTENT_AUTO_POST = {'/api/movements'}
IDEMPOTENT_PATCH_PREFIX = ('/api/items/', '/api/movements/', '/api/servers/',
                           '/api/material-spares/', '/api/server-spares/',
                           '/api/server-components/', '/api/users/')
SERVER_SPARE_STATUSES = ('待检测','可用备件','待RMA','RMA处理中','RMA已完成','报废','其他')
SPARE_NOTES_LIMIT = 500  # 物料坏件与服务器备件统一口径
ARCHIVED_SERVER_ID = -1
MATERIAL_SPARE_STATUSES = ('待处理','待RMA','RMA处理中','RMA已完成','可用备件','报废','其他')
IMPORT_LIMIT = 20 * 1024 * 1024
IMPORT_ROW_LIMIT = 10000
MAX_QTY = 10_000_000
SERVER_FIELD_LIMITS = {'room': 200, 'rack': 200, 'sn': 200}
IDEMPOTENCY_TTL = 7 * 24 * 3600
LOGIN_WINDOW = 300
LOGIN_MAX_FAILS = 5
LOGIN_BASE_LOCK = 60
LOGIN_MAX_LOCK = 900
FRAME_OPTIONS = os.environ.get('IDC_FRAME_OPTIONS', 'SAMEORIGIN')
HEADER_SCAN_ROWS = 30
IMPORT_TOKEN_MAX = 60
ITEM_FIELDS = ('category','model','description','planned','supplier','location','owner','notes')
CALC_FIELDS = ('received','used','damaged')
FIELD_LABELS = {'category':'类别','model':'型号','description':'名称/规格','planned':'计划数量',
                'supplier':'供应商/品牌','location':'存放位置','owner':'负责人','notes':'备注'}
FIELD_LIMITS = {'model':64,'description':200,'supplier':100,'location':100,'owner':100,'notes':500}
IGNORED_CALC_HEADERS = ('可用库存','未到数量','未到货数量','结存数量','现有库存','库存数量','剩余数量')
HEADER_ALIASES = {
    'model':['型号','物料型号','产品型号','规格型号','model','partnumber','pn','料号','物料编码'],
    'description':['名称规格','名称/规格','名称','品名','名称描述','物料名称','品名规格','description','descriptionspec','specification','spec'],
    'planned':['计划数量','计划qty','plannedqty','plannedquantity','采购数量','订购数量','计划采购数量','qty','quantity'],
    'category':['类别','物料类别','品类','分类','类型','category','type'],
    'supplier':['供应商品牌','供应商/品牌','供应商','品牌','supplier','brand'],
    'location':['存放位置','储存位置','仓库位置','库位','储位','storage location','location'],
    'owner':['负责人','责任人','owner'],
    'notes':['备注','说明','notes','remark','remarks'],
    'received':['累计入库','入库数量','receivedqty','receivedquantity'],
    'used':['已领用安装','已领用/安装','领用数量','usedinstalled','usedqty'],
    'damaged':['损坏报废','损坏/报废','报废数量','damagedqty','scrappedqty'],
}
MOVEMENT_FIELDS=('model','qty','category','date','location','owner','notes','kind')
MOVEMENT_LABELS={'model':'型号','qty':'入库数量','category':'类别','date':'日期',
                 'location':'位置/去向','owner':'负责人','notes':'备注','kind':'业务类型'}
MOVEMENT_HEADERS={
    'model':['型号','产品型号','设备型号','model','part number','pn'],
    'category':['类别','物料类别','类别分类','category'],
    'qty':['入库数量'],
    'date':['日期','入库日期','到货日期','收货日期','业务日期','date','received date'],
    'location':['位置/去向','存放位置','入库位置','库位','储位','position','location'],
    'owner':['负责人','责任人','经手人','操作人','登记人','owner','handler','operator'],
    'notes':['备注','说明','notes','remark'],
    'kind':['业务类型','出入库类型','变动类型','类型','出库类型','入库类型','库存变动类型','收发类型','单据类型','操作类型','transaction type','movement type'],
}
SERVER_FIELDS = ('room', 'rack', 'sn')
SERVER_LABELS = {'room': '机房/房间', 'rack': '机柜位置', 'sn': 'SN'}
SERVER_HEADERS = {
    'room': ['机房', '房间', '房間', 'room', 'datacenter', 'site', 'datahall',
             'data hall', 'hall', '机房名称', '区域'],
    'rack': ['rack', 'rack#', 'racknumber', '机柜', '机柜位置', '机柜编号',
             '机柜号', '机架位置', '位置', 'location'],
    'sn': ['sn', '序列号', '服务器序列号', '设备序列号', 'serialnumber',
           'serialno', 's/n', 'service tag', 'servicetag'],
}
COMPONENT_FIELDS = ('server_sn', 'node_sn', 'node', 'kp_name', 'bmc_mac')
COMPONENT_LABELS = {'server_sn': 'SN', 'node_sn': 'NODE_SN', 'node': 'NODE / U 位',
                    'kp_name': 'KP_NAME', 'bmc_mac': 'BMC_MAC'}
COMPONENT_HEADERS = {
    'server_sn': ['SN', 'SERVER_SN', 'SERVER_SERIAL_NUMBER', '服务器SN', '主机SN', '序列号'],
    'node_sn': ['NODE_SN', 'COMPONENT_SN', 'COMPONENT_SERIAL_NUMBER', '部件SN', '节点SN'],
    'node': ['NODE', 'U位', 'U位编号', 'UNIT_NUMBER', 'RU', 'U', '机位'],
    'kp_name': ['KP_NAME', 'PART_NAME', 'COMPONENT_NAME', '部件名称', '部件名'],
    'bmc_mac': ['BMC_MAC', 'BMC_MAC_ADDRESS', 'MAC', 'MAC地址'],
}
GB_SPECS = {
    'servers': dict(fields=SERVER_FIELDS, labels=SERVER_LABELS, headers=SERVER_HEADERS,
                    required=SERVER_FIELDS, store='SERVER_IMPORT_TOKENS'),
    # BMC_MAC 值可为空，列本身也不再是必需字段。
    'components': dict(fields=COMPONENT_FIELDS, labels=COMPONENT_LABELS,
                       headers=COMPONENT_HEADERS,
                       required=('server_sn', 'node_sn', 'node', 'kp_name'),
                       store='SERVER_COMPONENT_IMPORT_TOKENS'),
}
