# -*- coding: utf-8 -*-
"""validation.py：按职责拆分，显式依赖，无自导入/循环。"""

import re
import unicodedata
import math
from .config import MAX_QTY, FIELD_LABELS, FIELD_LIMITS, ITEM_FIELDS, SERVER_FIELD_LIMITS
def norm_header(value):
    return ''.join(ch.lower() for ch in str(value or '').strip() if ch.isalnum() or '\u3400' <= ch <= '\u9fff')

def norm_model(value):
    """型号唯一键：全角转半角 + 去首尾空格 + 大小写折叠。"""
    return unicodedata.normalize('NFKC', str(value or '')).strip().casefold()

def clean_text(value, field='notes'):
    text=str(value or '').strip()
    text=''.join(ch for ch in text if ch=='\t' or ord(ch)>=32)
    limit=FIELD_LIMITS.get(field,500)
    if len(text)>limit:raise ValueError(f'{FIELD_LABELS.get(field,field)}不能超过 {limit} 个字符')
    return text

def parse_planned(value, label='计划数量', allow_decimal_zero=False):
    """统一计划数量解析：0..MAX_QTY 的严格整数，拒绝布尔、浮点与非数字文本。
    allow_decimal_zero 仅供 Excel 单元格兼容 5.0 这类写法。"""
    if isinstance(value, bool):
        raise ValueError(f'{label}必须是 0 到 {MAX_QTY:,} 之间的整数，不接受布尔值')
    if isinstance(value, int):
        q = value
    elif isinstance(value, float):
        if allow_decimal_zero and math.isfinite(value) and value.is_integer():
            q = int(value)
        else:
            raise ValueError(f'{label}必须是 0 到 {MAX_QTY:,} 之间的整数，不接受小数')
    elif isinstance(value, str):
        s = value.strip().replace(',', '')
        pattern = r'\d+(?:\.0+)?' if allow_decimal_zero else r'\d+'
        if not re.fullmatch(pattern, s):
            raise ValueError(f'{label}必须是 0 到 {MAX_QTY:,} 之间的整数')
        q = int(float(s)) if '.' in s else int(s)
    else:
        raise ValueError(f'{label}必须是 0 到 {MAX_QTY:,} 之间的整数')
    if q < 0 or q > MAX_QTY:
        raise ValueError(f'{label}必须是 0 到 {MAX_QTY:,} 之间的整数')
    return q

def validate_server_fields(room, rack, sn):
    """服务器三个字段统一限长（与 Excel 导入口径一致），失败抛 ValueError。"""
    limits = SERVER_FIELD_LIMITS
    labels = {'room': '机房/房间', 'rack': '机柜位置', 'sn': 'SN'}
    for k, v in (('room', room), ('rack', rack), ('sn', sn)):
        if not v:
            raise ValueError('机房、机柜位置和 SN 均为必填项')
        if len(v) > limits[k]:
            raise ValueError(f'{labels[k]}不能超过 {limits[k]} 个字符')
    return room, rack, sn

def validate_item_fields(d, partial=False):
    """统一校验并规范化台账字段；partial=True 用于 PATCH。失败抛 ValueError。"""
    out={}
    for k in ITEM_FIELDS:
        if partial and k not in d:continue
        if k=='planned':
            out['planned']=parse_planned(d.get('planned',0))
        else:
            v=clean_text(d.get(k),k)
            if k in ('model','description') and not v:raise ValueError(f'{FIELD_LABELS[k]}不能为空')
            if k=='category' and v not in ('光模块','线缆'):raise ValueError('类别只能是光模块或线缆')
            out[k]=v
    if 'model' in out:out['model_key']=norm_model(out['model'])
    return out

def normalize_category(value):
    text=norm_header(value)
    if any(x in text for x in ('光模块','opticalmodule','transceiver')):return '光模块'
    if any(x in text for x in ('线缆','线材','cable','patchcord','网线')):return '线缆'
    if value in ('光模块','线缆'):return value
    return ''

def parse_movement_sn(value, qty, required=False):
    """库存明细 SN：入库/领用选填；报废必填且数量须等于 qty（逐件追溯）。
    多个 SN 用逗号、分号、顿号、换行或空白分隔；返回规范化的 SN 列表。
    """
    raw='' if value is None else str(value).strip()
    parts=[x.strip() for x in re.split(r'[,;\uff0c\u3001\s\r\n]+',raw) if x.strip()]
    if not required:
        if raw and len(raw)>2000:raise ValueError('SN 总长度不能超过 2000 字')
        for x in parts:
            if len(x)>150:raise ValueError('单个 SN 不能超过 150 字')
        return parts
    if len(parts)!=qty:
        raise ValueError(f'报废 {qty} 件必须填写 {qty} 个 SN（当前 {len(parts)} 个），请用逗号或换行分隔逐件填写')
    for x in parts:
        if len(x)>150:raise ValueError('单个 SN 不能超过 150 字')
    if len(set(parts))!=len(parts):raise ValueError('同一报废单内的 SN 不能重复')
    return parts

def parse_qty(value, label='数量'):
    """严格正整数：拒绝小数、布尔、非数字字符串、零/负数与超大值。"""
    if isinstance(value, bool):raise ValueError(f'{label}必须是正整数，不接受布尔值')
    if isinstance(value, float):raise ValueError(f'{label}不允许小数')
    if isinstance(value, int):q=value
    elif isinstance(value, str):
        s=value.strip()
        if not re.fullmatch(r'\d+',s):raise ValueError(f'{label}必须是正整数')
        q=int(s)
    else:raise ValueError(f'{label}必须是正整数')
    if q<=0 or q>MAX_QTY:raise ValueError(f'{label}必须是 1 到 {MAX_QTY:,} 之间的整数')
    return q
