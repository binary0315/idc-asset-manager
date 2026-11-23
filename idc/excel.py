# -*- coding: utf-8 -*-
"""excel.py：按职责拆分，显式依赖，无自导入/循环。"""

import secrets
import time
import zipfile
import io
import posixpath
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime, timedelta
from .config import CALC_FIELDS, FIELD_LABELS, FIELD_LIMITS, GB_SPECS, HEADER_ALIASES, HEADER_SCAN_ROWS, IGNORED_CALC_HEADERS, IMPORT_LIMIT, IMPORT_ROW_LIMIT, IMPORT_TOKEN_MAX, ITEM_FIELDS, MOVEMENT_FIELDS, MOVEMENT_HEADERS, MOVEMENT_LABELS, SERVER_FIELD_LIMITS
from .state import IMPORT_TOKENS, MOVEMENT_IMPORT_TOKENS, SERVER_COMPONENT_IMPORT_TOKENS, SERVER_IMPORT_TOKENS
from .db import db, db_ctx
from .validation import norm_header, norm_model, clean_text, parse_planned, parse_qty, normalize_category, validate_server_fields
def prune_import_tokens(now=None):
    now=now or int(time.time())
    for store in (IMPORT_TOKENS,MOVEMENT_IMPORT_TOKENS,SERVER_IMPORT_TOKENS,SERVER_COMPONENT_IMPORT_TOKENS):
        for key in [k for k,v in store.items() if v.get('expires',0)<now]:store.pop(key,None)
        if len(store)>IMPORT_TOKEN_MAX:
            for key in sorted(store,key=lambda k:store[k].get('expires',0))[:len(store)-IMPORT_TOKEN_MAX]:store.pop(key,None)

def match_header(value):
    text=norm_header(value)
    if not text:return None,[]
    if any(norm_header(w) in text for w in IGNORED_CALC_HEADERS):return None,[]
    exact={field for field,aliases in HEADER_ALIASES.items() if text in {norm_header(a) for a in aliases}}
    if len(exact)==1:return next(iter(exact)),[]
    matches=[]
    for field,aliases in HEADER_ALIASES.items():
        for alias in aliases:
            key=norm_header(alias)
            if not key:continue
            min_len=2 if any('\u3400' <= ch <= '\u9fff' for ch in key) else 3
            if len(key)>=min_len and key in text:
                matches.append((len(key),field,key))
    if not matches:return None,[]
    top=max(x[0] for x in matches); fields=sorted({x[1] for x in matches if x[0]==top})
    if len(fields)>1:return None,fields
    return fields[0],[]

def xlsx_matrix(data):
    if len(data)>IMPORT_LIMIT: raise ValueError('文件超过 20 MB 限制')
    try: z=zipfile.ZipFile(io.BytesIO(data))
    except Exception: raise ValueError('文件不是有效的 .xlsx 工作簿')
    with z:
        infos=z.infolist()
        if sum(x.file_size for x in infos)>80*1024*1024: raise ValueError('Excel 解压后超过 80 MB 限制')
        names=set(z.namelist())
        if 'xl/workbook.xml' not in names: raise ValueError('无法读取 Excel 工作簿结构')
        ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships','rel':'http://schemas.openxmlformats.org/package/2006/relationships'}
        book=ET.fromstring(z.read('xl/workbook.xml')); sheet=book.find('m:sheets/m:sheet',ns)
        if sheet is None: raise ValueError('工作簿没有工作表')
        rid=sheet.attrib.get('{%s}id'%ns['r'])
        relroot=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target=next((x.attrib.get('Target','') for x in relroot if x.attrib.get('Id')==rid),'')
        if not target: raise ValueError('找不到第一个工作表')
        sheetpath=posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('xl',target))
        if sheetpath not in names: raise ValueError('无法读取第一个工作表')
        shared=[]
        if 'xl/sharedStrings.xml' in names:
            root=ET.fromstring(z.read('xl/sharedStrings.xml'))
            shared=[''.join(t.text or '' for t in si.findall('.//m:t',ns)) for si in root.findall('m:si',ns)]
        root=ET.fromstring(z.read(sheetpath)); matrix=[]
        def colnum(ref):
            letters=re.match(r'[A-Z]+',ref or 'A').group(0); n=0
            for ch in letters:n=n*26+ord(ch)-64
            return n-1
        for row in root.findall('.//m:sheetData/m:row',ns):
            vals=[]
            for cell in row.findall('m:c',ns):
                ix=colnum(cell.attrib.get('r','A1')); vals.extend(['']*max(0,ix-len(vals)+1))
                typ=cell.attrib.get('t'); v=cell.find('m:v',ns)
                if typ=='inlineStr': value=''.join(t.text or '' for t in cell.findall('.//m:t',ns))
                elif v is None:value=''
                elif typ=='s':
                    try:value=shared[int(v.text)]
                    except:value=''
                elif typ=='b':value='TRUE' if v.text=='1' else 'FALSE'
                else:value=v.text or ''
                if ix>=len(vals): vals.extend(['']*(ix-len(vals)+1))
                vals[ix]=value
            matrix.append((int(row.attrib.get('r',str(len(matrix)+1))),vals))
            if len(matrix)>IMPORT_ROW_LIMIT+20:raise ValueError('工作表行数超过 10,000 行限制')
        return matrix

def item_matrix(data):
    matrix=xlsx_matrix(data)
    nonempty=[(rn,row) for rn,row in matrix if any(str(v).strip() for v in row)]
    if not nonempty:raise ValueError('工作表为空')
    return matrix,nonempty

def active_column_indexes(columns,nonempty,header_row):
    """只保留有内容的列：表头非空，或任意数据行该列有值。纯空列不进入字段映射界面。"""
    data_rows=[row for rn,row in nonempty if rn>header_row]
    return [i for i,v in enumerate(columns)
            if str(v).strip() or any(i<len(r) and str(r[i]).strip() for r in data_rows)]

def header_candidates(nonempty):
    """前 30 个非空行都可被用户选作表头行。"""
    return {rn:[str(v).strip() for v in row] for rn,row in nonempty[:HEADER_SCAN_ROWS]}

def auto_detect_header(candidates):
    best_rn=None; best_score=-1; best_map={}; best_issues=[]
    for rn,headers in candidates.items():
        found={}; issues=[]
        for ix,val in enumerate(headers):
            field,ambiguous=match_header(val)
            if ambiguous:
                issues.append(f'表头「{val}」可能对应多个字段：'+ '、'.join(FIELD_LABELS.get(x,x) for x in ambiguous))
            if field:
                if field in found:
                    issues.append(f'表头「{val}」与前面的列冲突：都对应「{FIELD_LABELS.get(field,field)}」')
                else:found[field]=ix
        score=len(found)+(10 if 'model' in found and 'description' in found else 0)
        if score>best_score:best_rn,best_score,best_map,best_issues=rn,score,found,issues
    return best_rn,best_map,best_issues

def build_item_preview(batch, header_row, mapping, token, header_issues=None):
    """按显式表头行 + 字段映射解析数据行；自动识别与手动改映射共用这一套校验。"""
    nonempty=batch['nonempty']; candidates=batch['header_candidates']
    default_category=batch['default_category']; mode=batch['mode']
    if header_row not in candidates:raise ValueError('请选择有效的表头所在行')
    columns=list(candidates[header_row])
    width=max([len(columns)]+[len(row) for _,row in nonempty])
    columns+=['']*(width-len(columns))
    active=active_column_indexes(columns,nonempty,header_row)
    errors=list(dict.fromkeys(header_issues or [])); warnings=[]
    required=('model','description','planned') if mode=='create' else ('model',)
    missing=[f for f in required if f not in mapping]
    category_missing=(mode=='create' and 'category' not in mapping
                      and default_category not in ('光模块','线缆'))
    if category_missing:missing.append('category')
    hard_missing=[f for f in missing if f!='planned']
    if hard_missing:
        errors.append('缺少必需字段：'+ '、'.join(FIELD_LABELS.get(x,x) for x in dict.fromkeys(hard_missing))
                      +'。可在下方「字段对应」中手动指定对应列，或改选表头所在行。')
    if 'planned' in missing:
        warnings.append('表格未提供「计划数量」列，预览中暂填 0；请在下方逐行填写需要的数量，或删除不需要的行后确认导入。')
    movement_like=[str(v) for v in columns if str(v).strip()
                   and any(norm_header(w)==norm_header(v) for w in
                           ('入库数量','到货数量','已领用','领用/安装','领用数量','报废数量','损坏报废'))]
    suggest_import='movements' if movement_like else ''
    if movement_like:
        warnings.append('检测到「'+'、'.join(movement_like[:5])+'」等库存变动特征列：这更像入库明细，而不是物料台账。'
                        '这些列不会被写入物料台账；如选错类型，请切换到「入库明细」导入。')
    ignored_calc=[str(v) for v in columns if str(v).strip()
                  and any(norm_header(w) in norm_header(v) for w in IGNORED_CALC_HEADERS)]
    ignored_calc+= [str(columns[i]) for f,i in mapping.items()
                    if f in CALC_FIELDS and 0<=i<len(columns)]
    ignored_calc=list(dict.fromkeys(x for x in ignored_calc if x))
    if ignored_calc:
        warnings.append('以下库存/计算列已忽略，不会写入库存变动明细：'+ '、'.join(ignored_calc[:12]))
    mapped_cols={i for f,i in mapping.items() if f in ITEM_FIELDS}
    calc_cols={mapping.get(f) for f in CALC_FIELDS}
    unknown=[str(v) for i,v in enumerate(columns) if i not in mapped_cols and str(v).strip()
             and i not in calc_cols
             and not any(norm_header(w) in norm_header(v) for w in IGNORED_CALC_HEADERS)]
    if unknown:warnings.append('未识别的列将忽略：'+ '、'.join(unknown[:12])+('…' if len(unknown)>12 else ''))
    with db_ctx() as c:
        existing={r['model_key'] for r in c.execute('SELECT model_key FROM items')}
        existing_by_key=({r['model_key']:dict(r) for r in c.execute('SELECT * FROM items')}
                         if mode=='update' else {})
    rows=[]; seen=set()
    for rn,row in [(rn,row) for rn,row in nonempty if rn>header_row]:
        if not any(str(v).strip() for v in row):continue
        def get(field):
            ix=mapping.get(field,-1)
            return str(row[ix] if 0<=ix<len(row) else '').strip()
        issues=[]; model=get('model'); key=norm_model(model)
        if not model:issues.append('型号为空')
        base=existing_by_key.get(key) if mode=='update' else None
        if mode=='update' and model and not base:
            issues.append('台账中找不到该型号；更新模式只能修改已存在的物料')
        if model and key in seen:issues.append('文件内型号重复（忽略大小写后）')
        seen.add(key)
        if mode=='create' and model and key in existing:issues.append('台账中已存在该型号')
        record={'row':rn,'model':model,'model_key':key}
        if mode=='update':record['matched_id']=base['id'] if base else None
        for field in ITEM_FIELDS:
            if field=='model':continue
            raw=get(field) if field in mapping else ''
            if mode=='update' and not raw and base:raw=str(base.get(field) or '')
            if field=='planned':
                if not raw:
                    record['planned']=int(base['planned']) if (mode=='update' and base) else 0
                    if field in mapping:issues.append('计划数量为空')
                else:
                    try:
                        record['planned']=parse_planned(raw, allow_decimal_zero=True)
                    except ValueError as e:
                        record['planned']=0; issues.append(str(e))
            elif field=='category':
                cat=normalize_category(raw or default_category)
                if not cat:
                    if mode=='create' and not category_missing:
                        issues.append('类别无法识别，请修正类别列或选择整批默认类别')
                    cat=str(base['category']) if (mode=='update' and base) else '光模块'
                record['category']=cat
            else:
                try:record[field]=clean_text(raw,field)
                except ValueError as e:
                    record[field]=str(raw)[:FIELD_LIMITS.get(field,500)]; issues.append(str(e))
        if mode=='create' and not record.get('description'):issues.append('名称/规格为空')
        if issues:errors.append(f'第 {rn} 行：'+'；'.join(issues))
        rows.append(record)
    if len(errors)>100:errors=errors[:100]+[f'另有 {len(errors)-100} 条错误未显示']
    if not rows:errors.append('表头之后没有可导入的数据行')
    if len(rows)>IMPORT_ROW_LIMIT:errors.append('数据行数超过 10,000 行限制')
    can_import=not errors and bool(rows)
    batch.update({'header_row':header_row,'mapping':mapping,'rows':rows,
                  'mode':mode,'columns':columns})
    return {'header_row':header_row,'columns':columns,
            'active_columns':active,'hidden_columns':len(columns)-len(active),
            'header_candidates':[{'row':rn,'cells':cells[:12]} for rn,cells in candidates.items()],
            'mapping':{f:(columns[i] if 0<=i<len(columns) else str(i)) for f,i in mapping.items()},
            'mapping_index':dict(mapping),'mode':mode,'rows':len(rows),
            'errors':errors,'warnings':warnings,'samples':rows[:8],'preview_rows':rows,
            'suggest_import':suggest_import,
            'token':token if can_import else None,'edit_token':token,'can_import':can_import}

def parse_item_import(data, default_category, user_id, mode='create'):
    matrix,nonempty=item_matrix(data)
    candidates=header_candidates(nonempty)
    header_row,mapping,issues=auto_detect_header(candidates)
    token=secrets.token_urlsafe(24)
    batch={'user_id':user_id,'expires':int(time.time())+1200,'matrix':matrix,'nonempty':nonempty,
           'header_candidates':candidates,'default_category':default_category,'mode':mode,
           'header_row':header_row,'mapping':mapping,'rows':[],'columns':candidates[header_row]}
    result=build_item_preview(batch,header_row,mapping,token,header_issues=issues)
    prune_import_tokens()
    IMPORT_TOKENS[token]=batch
    return result

def remap_item_import(batch, header_row, mapping, token):
    return build_item_preview(batch,header_row,mapping,token)

def movement_header(value):
    text=norm_header(value)
    if not text:return None,[]
    if str(value or '').strip()=='入库数量':return 'qty',[]
    if any(word in text for word in ('计划','planned','采购计划')) and any(word in text for word in ('数量','qty','quantity')):
        return None,['计划数量不能用作本次入库数量']
    matches=[]
    for field,aliases in MOVEMENT_HEADERS.items():
        for alias in aliases:
            key=norm_header(alias)
            if field=='qty':continue
            if key and key in text:matches.append((len(key),field))
    if not matches:return None,[]
    best=max(n for n,_ in matches); fields=sorted({f for n,f in matches if n==best})
    if len(fields)>1:return None,fields
    return fields[0],[]

def movement_date(value):
    value=str(value or '').strip()
    if not value:return date.today().isoformat(),True
    try:
        serial=float(value)
        if 1<=serial<=100000:return (datetime(1899,12,30)+timedelta(days=serial)).date().isoformat(),False
    except:pass
    for fmt in ('%Y-%m-%d','%Y/%m/%d','%Y.%m.%d','%Y%m%d','%Y-%m-%d %H:%M:%S'):
        try:return datetime.strptime(value,fmt).date().isoformat(),False
        except:pass
    raise ValueError('日期格式无法识别')

def inbound_kind(value):
    text=norm_header(value)
    inbound={'入库','收货','到货','采购入库','退库','inbound','receipt','received'}
    outbound=('出库','领用','安装','报废','损耗','损坏','退货','outbound','issue','scrap','damage','loss')
    if any(x in text for x in outbound):return False
    return any(x in text for x in inbound)

def movement_header_candidates(nonempty):
    return {rn:[str(v).strip() for v in row] for rn,row in nonempty[:HEADER_SCAN_ROWS]}

def auto_detect_movement_header(candidates):
    best_rn=None; best_score=-1; best_map={}; best_issues=[]
    for rn,headers in candidates.items():
        found={}; issues=[]
        for ix,val in enumerate(headers):
            field,amb=movement_header(val)
            if amb:
                issues.append(f'表头「{val}」无法唯一识别：'+ '、'.join(MOVEMENT_LABELS.get(x,x) for x in amb))
            if field:
                if field in found:
                    issues.append(f'表头「{val}」与前面的列冲突：都对应「{MOVEMENT_LABELS.get(field,field)}」')
                else:found[field]=ix
        score=len(found)+(8 if 'model' in found and 'qty' in found else 0)
        if score>best_score:best_rn,best_score,best_map,best_issues=rn,score,found,issues
    return best_rn,best_map,best_issues

def select_edited_rows(edited, original):
    """预览允许删除行：edited 为 1..len(original) 条。

    带 row（Excel 原始行号）时按身份对齐 base，禁止伪造或重复；
    不带 row 的旧客户端按位置取前 N 条，保持兼容。
    """
    if not isinstance(edited, list):
        raise ValueError('编辑后的数据格式不正确，请重新上传文件')
    if not edited:
        raise ValueError('预览中没有可导入的数据；可点「恢复已删除的行」，或重新上传文件')
    if len(edited) > len(original):
        raise ValueError('编辑后的行数超过预览行数，请重新上传文件')
    if any(not isinstance(r, dict) for r in edited):
        raise ValueError('编辑后的数据格式不正确，请重新上传文件')
    keys = [r.get('row') for r in edited]
    if all(k is not None for k in keys):
        orig_rows = [r.get('row') for r in original]
        if len(set(keys)) != len(keys) or any(k not in orig_rows for k in keys):
            raise ValueError('编辑后的行与预览不一致，请重新上传文件')
        by_row = {r.get('row'): r for r in original}
        return [by_row[k] for k in keys]
    return original[:len(edited)]

def build_movement_preview(batch,header_row,mapping,token,header_issues=None):
    """自动识别与手动改映射共用这一套校验；必需字段只有 型号 + 入库数量。"""
    nonempty=batch['nonempty']; candidates=batch['header_candidates']
    default_category=batch.get('default_category','')
    if header_row not in candidates:raise ValueError('请选择有效的表头所在行')
    columns=list(candidates[header_row])
    width=max([len(columns)]+[len(row) for _,row in nonempty])
    columns+=['']*(width-len(columns))
    active=active_column_indexes(columns,nonempty,header_row)
    errors=list(dict.fromkeys(header_issues or [])); warnings=[]
    missing=[f for f in ('model','qty') if f not in mapping]
    qty_missing='qty' in missing
    hard_missing=[f for f in missing if f!='qty']
    if hard_missing:
        errors.append('缺少必需字段：'+ '、'.join(MOVEMENT_LABELS.get(x,x) for x in hard_missing)
                      +'。请在下方「字段对应」中手动指定对应列，或改选表头所在行。')
    if qty_missing:
        warnings.append('表格未提供「入库数量」列，预览中暂填 0；请在下方逐行填写大于 0 的整数，或删除不需要的行后确认导入。')
    mapped_cols={i for f,i in mapping.items() if f in MOVEMENT_FIELDS}
    unknown=[str(v) for i,v in enumerate(columns) if i not in mapped_cols and str(v).strip()]
    if unknown:warnings.append('未识别的列将忽略：'+ '、'.join(unknown[:12])+('…' if len(unknown)>12 else ''))
    if 'kind' not in mapping:warnings.append('表内未提供业务类型列，本批将统一按「入库」处理。')
    if 'date' not in mapping:warnings.append('表内未提供日期列，日期将使用导入当天。')
    if 'qty' in mapping and 0<=mapping['qty']<len(columns):
        qcol=str(columns[mapping['qty']] or '')
        if qcol and qcol.strip()!='入库数量' and ('计划' in qcol or 'planned' in norm_header(qcol)):
            warnings.append(f'数量列「{qcol}」含「计划」字样，请确认它确实是入库数量。')
    with db_ctx() as c:
        items={norm_model(r['model']):dict(r) for r in c.execute('SELECT id,category,model,description FROM items')}
    rows=[]; used_today=False; need_qty=0
    for rn,row in [(rn,row) for rn,row in nonempty if rn>header_row]:
        if not any(str(v).strip() for v in row):continue
        def get(field):
            ix=mapping.get(field,-1)
            return str(row[ix] if 0<=ix<len(row) else '').strip()
        issues=[]; model=get('model'); key=norm_model(model)
        if not model:issues.append('型号为空')
        if qty_missing:
            qty=0; need_qty+=1
        else:
            try:qty=parse_qty(get('qty').replace(',',''),'入库数量')
            except ValueError as e:qty=0;issues.append(str(e))
        base=items.get(key)
        if model and not base:issues.append('台账中找不到该型号（型号需与台账一致）')
        category=str(base['category']) if base else (get('category') or default_category or '光模块')
        rawcat=get('category')
        if base and rawcat:
            cn=normalize_category(rawcat)
            if cn!=base['category']:issues.append('类别与该型号台账不一致')
        if 'kind' in mapping:
            k=get('kind')
            if not k:issues.append('业务类型为空')
            elif not inbound_kind(k):issues.append(f'业务类型「{k}」不是入库；本功能仅导入入库')
        try:
            moved_at,today=movement_date(get('date'))
            if today:used_today=True
        except ValueError as e:
            moved_at=date.today().isoformat();issues.append(str(e))
        try:
            loc=clean_text(get('location'),'location')
            own=clean_text(get('owner'),'owner')
            nt=clean_text(get('notes'),'notes')
        except ValueError as e:
            loc=get('location');own=get('owner');nt=get('notes');issues.append(str(e))
        sn=get('sn')
        if len(sn)>2000:sn=sn[:2000];issues.append('SN 过长已截断')
        record={'item_id':base['id'] if base else None,
                'category':category,'model':base['model'] if base else model,
                'description':base['description'] if base else '',
                'kind':'入库','qty':qty,'moved_at':moved_at,'sn':sn,
                'location':loc,'owner':own,'notes':nt,'row':rn}
        if issues:errors.append(f'第 {rn} 行：'+'；'.join(issues))
        rows.append(record)
    if used_today:warnings.append('缺失日期的记录将使用导入当天日期。')
    if qty_missing and need_qty:
        warnings.append(f'有 {need_qty} 行尚未填写入库数量；请在预览表格中补填大于 0 的整数，或删除不需要的行。确认时会再完整校验一次。')
    if len(errors)>100:errors=errors[:100]+[f'另有 {len(errors)-100} 条错误未显示']
    if not rows:errors.append('表头之后没有可导入的入库记录')
    if len(rows)>IMPORT_ROW_LIMIT:errors.append('数据行数超过 10,000 行限制')
    can_import=not errors and bool(rows)
    batch.update({'header_row':header_row,'mapping':mapping,'rows':rows,'columns':columns})
    return {'header_row':header_row,'columns':columns,
            'active_columns':active,'hidden_columns':len(columns)-len(active),
            'header_candidates':[{'row':rn,'cells':cells[:12]} for rn,cells in candidates.items()],
            'mapping':{f:(columns[i] if 0<=i<len(columns) else str(i)) for f,i in mapping.items()},
            'mapping_index':dict(mapping),'rows':len(rows),'errors':errors,'warnings':warnings,
            'samples':rows[:8],'preview_rows':rows,
            'qty_missing':qty_missing,'need_qty':need_qty,
            'token':token if can_import else None,'edit_token':token,'can_import':can_import}

def parse_movement_import(data,user_id,default_category):
    matrix,nonempty=item_matrix(data)
    candidates=movement_header_candidates(nonempty)
    header_row,mapping,issues=auto_detect_movement_header(candidates)
    token=secrets.token_urlsafe(24)
    batch={'user_id':user_id,'expires':int(time.time())+1200,'matrix':matrix,'nonempty':nonempty,
           'header_candidates':candidates,'default_category':default_category,
           'header_row':header_row,'mapping':mapping,'rows':[],'columns':candidates[header_row]}
    result=build_movement_preview(batch,header_row,mapping,token,header_issues=issues)
    prune_import_tokens()
    MOVEMENT_IMPORT_TOKENS[token]=batch
    return result

def remap_movement_import(batch,header_row,mapping,token):
    return build_movement_preview(batch,header_row,mapping,token)

def gb_header_match(value, headers_map):
    """先精确匹配，再按最长别名子串匹配；一个表头对多个字段时视为不明确。"""
    text = norm_header(value)
    if not text:
        return None, []
    exact = {f for f, aliases in headers_map.items()
             if text in {norm_header(a) for a in aliases}}
    if len(exact) == 1:
        return next(iter(exact)), []
    matches = []
    for field, aliases in headers_map.items():
        for alias in aliases:
            key = norm_header(alias)
            if not key:
                continue
            min_len = 2 if any('\u3400' <= ch <= '\u9fff' for ch in key) else 3
            if len(key) >= min_len and key in text:
                matches.append((len(key), field, key))
    if not matches:
        return None, []
    top = max(x[0] for x in matches)
    fields = sorted({x[1] for x in matches if x[0] == top})
    return (fields[0], []) if len(fields) == 1 else (None, fields)

def auto_detect_gb_header(candidates, spec):
    """两阶段表头识别：精确别名先占位，再只对仍缺失的字段做模糊匹配。
    gbgInfo 一类的导出表含 NODE_SN_KP、BF3_BMC_MAC、OOB_OS_MAC、CX8_*_MAC 等衍生列，
    旧逻辑用子串「先到先得」会让它们抢走 NODE_SN / BMC_MAC，随后精确列反被判成冲突，
    整批被阻断。现在这些列一律忽略并给警告，不产生错误。"""
    headers_map = spec['headers']
    labels = spec['labels']
    exact_alias = {}
    for field, aliases in headers_map.items():
        for alias in aliases:
            key = norm_header(alias)
            if key:
                exact_alias.setdefault(key, field)
    mac_exact = {norm_header(a) for a in headers_map.get('bmc_mac', [])}
    best_rn = None; best_score = -1; best_map = {}; best_issues = []; best_warnings = []

    for rn, headers in candidates.items():
        found = {}; issues = []; warnings = []; used = set()
        # 阶段 A：全表扫描精确匹配，精确列优先占位
        for ix, val in enumerate(headers):
            text = norm_header(val)
            if not text:
                continue
            field = exact_alias.get(text)
            if field is None:
                continue
            if field in found:
                warnings.append('表头「%s」与「%s」都对应「%s」，已保留前一列，本列忽略。'
                                % (val, headers[found[field]], labels.get(field, field)))
                continue
            found[field] = ix; used.add(ix)

        # 阶段 B：只对仍缺失的字段做模糊匹配；衍生列与额外 MAC 列忽略并警告
        remaining = {f: a for f, a in headers_map.items() if f not in found}
        for ix, val in enumerate(headers):
            if ix in used:
                continue
            text = norm_header(val)
            if not text:
                continue
            # 无论是否还有缺失字段，衍生列与额外 MAC 列都要忽略并给出可见警告。
            if 'mac' in text and text not in mac_exact:
                warnings.append('已忽略额外的 MAC 列「%s」（仅精确的 BMC_MAC 列会用于映射）。' % val)
                continue
            # 列文本里含有已精确命中字段的长别名 → 视为衍生列（如 NODE_SN_KP）
            derived = False
            for field, aliases in headers_map.items():
                if field not in found:
                    continue
                if any(len(norm_header(a)) >= 4 and norm_header(a) in text for a in aliases):
                    derived = True; break
            if derived:
                warnings.append('已忽略衍生列「%s」（相关字段已有精确列）。' % val)
                continue
            if not remaining:
                continue
            field, ambiguous = gb_header_match(val, remaining)
            if ambiguous:
                warnings.append('表头「%s」可能对应多个字段，已忽略：' % val
                                + '、'.join(labels.get(x, x) for x in ambiguous))
                continue
            if field:
                found[field] = ix; used.add(ix)
                remaining = {f: a for f, a in headers_map.items() if f not in found}

        score = len(found) + (8 if all(k in found for k in spec['required']) else 0)
        if score > best_score:
            best_rn, best_score, best_map = rn, score, found
            best_issues, best_warnings = issues, warnings
    return best_rn, best_map, best_issues, best_warnings

def gb_context(kind):
    with db_ctx() as c:
        existing = {r['sn'] for r in c.execute('SELECT sn FROM servers')}
        if kind == 'servers':
            return {'existing': existing, 'seen': set()}
        return {'existing': existing,
                'existing_details': {r['server_sn'] for r in c.execute(
                    'SELECT DISTINCT server_sn FROM server_components')},
                'seen_nodes': set(), 'seen_node_sns': set(),
                'counts': {}, 'serials': set()}

def parse_server_row(get, rn, ctx):
    room, rack, sn = get('room'), get('rack'), get('sn')
    issues = []
    for label, val in (('机房/房间', room), ('机柜位置', rack), ('SN', sn)):
        if not val:
            issues.append(label + '为空')
        elif len(val) > max(SERVER_FIELD_LIMITS.values()):
            issues.append(label + f'不能超过 {max(SERVER_FIELD_LIMITS.values())} 个字符')
    if sn and sn in ctx['seen']:
        issues.append('本文件中 SN 重复')
    if sn and sn in ctx['existing']:
        issues.append('系统中已存在该 SN')
    ctx['seen'].add(sn)
    return {'room': room, 'rack': rack, 'sn': sn}, issues

def parse_component_row(get, rn, ctx):
    server_sn = get('server_sn'); node_sn = get('node_sn')
    node_raw = get('node'); kp_name = get('kp_name'); bmc_mac = get('bmc_mac')
    issues = []; node_u = None
    if not server_sn:
        issues.append('SN 为空')
    elif server_sn not in ctx['existing']:
        issues.append('SN 未在服务器位置台账中找到')
    if not node_sn:
        issues.append('NODE_SN 为空')
    if not kp_name:
        issues.append('KP_NAME 为空')
    for label, val in (('SN', server_sn), ('NODE_SN', node_sn),
                       ('KP_NAME', kp_name), ('BMC_MAC', bmc_mac)):
        if len(val) > 200:
            issues.append(label + '不能超过 200 个字符')
    try:
        number = float(node_raw.replace(',', ''))
        if not number.is_integer() or number < 1 or number > 46:
            raise ValueError()
        node_u = int(number)
    except (ValueError, TypeError, AttributeError):
        issues.append('NODE 必须是 1 到 46 的整数 U 位')
    if server_sn and node_u is not None:
        key = (server_sn, node_u)
        if key in ctx['seen_nodes']:
            issues.append('同一 SN 的 NODE U 位重复')
        ctx['seen_nodes'].add(key)
        ctx['counts'][server_sn] = ctx['counts'].get(server_sn, 0) + 1
    if server_sn and node_sn:
        key = (server_sn, node_sn)
        if key in ctx['seen_node_sns']:
            issues.append('同一 SN 的 NODE_SN 重复')
        ctx['seen_node_sns'].add(key)
    if server_sn:
        ctx['serials'].add(server_sn)
    return {'server_sn': server_sn, 'node_sn': node_sn, 'node_u': node_u or 0,
            'kp_name': kp_name, 'bmc_mac': bmc_mac}, issues

def build_gb_preview(batch, header_row, mapping, token, kind,
                     header_issues=None, header_warnings=None):
    """自动识别与手动改映射共用同一套校验；纯空列自动隐藏，映射用原始列索引。"""
    spec = GB_SPECS[kind]
    nonempty = batch['nonempty']; candidates = batch['header_candidates']
    if header_row not in candidates:
        raise ValueError('请选择有效的表头所在行')
    columns = list(candidates[header_row])
    width = max([len(columns)] + [len(row) for _, row in nonempty])
    columns += [''] * (width - len(columns))
    active = active_column_indexes(columns, nonempty, header_row)
    errors = list(dict.fromkeys(header_issues or []))
    warnings = list(dict.fromkeys(header_warnings or []))
    missing = [f for f in spec['required'] if f not in mapping]
    if missing:
        errors.append('缺少必需字段：'
                      + '、'.join(spec['labels'][f] for f in missing)
                      + '。可在下方「字段对应」中手动指定对应列，或改选表头所在行。')
    mapped_cols = {i for f, i in mapping.items() if f in spec['fields']}
    unknown = [str(v) for i, v in enumerate(columns)
               if i not in mapped_cols and str(v).strip()]
    if unknown:
        warnings.append('未识别的列将忽略：' + '、'.join(unknown[:12])
                        + ('…' if len(unknown) > 12 else ''))
    if kind == 'components' and 'bmc_mac' not in mapping:
        warnings.append('未提供 BMC_MAC 列，导入后这些部件的 MAC 为空（BMC_MAC 为可选字段）。')
    ctx = gb_context(kind)
    rows = []
    for rn, row in [(rn, row) for rn, row in nonempty if rn > header_row]:
        if not any(str(v).strip() for v in row):
            continue
        def get(field):
            ix = mapping.get(field, -1)
            raw = row[ix] if 0 <= ix < len(row) else ''
            return str(raw if raw is not None else '').strip()
        record, issues = (parse_server_row if kind == 'servers'
                          else parse_component_row)(get, rn, ctx)
        record['row'] = rn
        if issues:
            errors.append(f'第 {rn} 行：' + '；'.join(issues))
        rows.append(record)
    server_count = 0
    if kind == 'components':
        unusual = [sn for sn, count in ctx['counts'].items() if count != 37]
        if unusual:
            warnings.append('部分 SN 的部件数不是预期的 37 个 1U 部件；请确认该 SN 的记录是否完整。')
        replacing = ctx['serials'] & ctx['existing_details']
        if replacing:
            warnings.append(f'确认后将替换 {len(replacing)} 台服务器现有的 46U 部件清单。')
        server_count = len(ctx['serials'])
    if len(errors) > 100:
        errors = errors[:100] + [f'另有 {len(errors) - 100} 条错误未显示']
    if not rows:
        errors.append('表头之后没有可导入的数据行' if kind == 'servers'
                      else '表头之后没有可导入的部件记录')
    if len(rows) > IMPORT_ROW_LIMIT:
        errors.append('数据行数超过 10,000 行限制')
    can_import = not errors and bool(rows)
    batch.update({'header_row': header_row, 'mapping': mapping,
                  'rows': rows, 'columns': columns})
    if kind == 'components':
        batch['serials'] = sorted(ctx['serials'])
    result = {'header_row': header_row, 'columns': columns,
              'active_columns': active,
              'hidden_columns': len(columns) - len(active),
              'header_candidates': [{'row': rn, 'cells': cells[:12]}
                                    for rn, cells in candidates.items()],
              'mapping': {f: (columns[i] if 0 <= i < len(columns) else str(i))
                          for f, i in mapping.items()},
              'mapping_index': dict(mapping), 'rows': len(rows),
              'errors': errors, 'warnings': warnings,
              'samples': rows[:8], 'preview_rows': rows,
              'token': token if can_import else None,
              'edit_token': token, 'can_import': can_import}
    if kind == 'components':
        result['servers'] = server_count
    return result

def parse_gb_import(data, user_id, kind):
    matrix, nonempty = item_matrix(data)
    candidates = header_candidates(nonempty)
    header_row, mapping, issues, header_warnings = auto_detect_gb_header(candidates, GB_SPECS[kind])
    token = secrets.token_urlsafe(24)
    batch = {'user_id': user_id, 'expires': int(time.time()) + 1200,
             'matrix': matrix, 'nonempty': nonempty,
             'header_candidates': candidates, 'header_row': header_row,
             'mapping': mapping, 'rows': [], 'columns': candidates[header_row],
             'kind': kind}
    if kind == 'components':
        batch['serials'] = []
    result = build_gb_preview(batch, header_row, mapping, token, kind,
                              header_issues=issues, header_warnings=header_warnings)
    prune_import_tokens()
    (SERVER_IMPORT_TOKENS if kind == 'servers'
     else SERVER_COMPONENT_IMPORT_TOKENS)[token] = batch
    return result

def remap_gb_import(batch, header_row, mapping, token):
    return build_gb_preview(batch, header_row, mapping, token, batch['kind'])

def make_xlsx(sheet_name,headers,rows,widths=None):
    def colname(n):
        out=''
        while n:
            n,rem=divmod(n-1,26);out=chr(65+rem)+out
        return out
    def cell(ref,value,header=False):
        style=' s="1"' if header else ''
        if value is None:return f'<c r="{ref}"{style}/>'
        if isinstance(value,bool):return f'<c r="{ref}" t="b"{style}><v>{1 if value else 0}</v></c>'
        if isinstance(value,(int,float)):return f'<c r="{ref}"{style}><v>{value}</v></c>'
        text=xml_escape(str(value))
        return f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{text}</t></is></c>'
    sheet_rows=[]
    for ri,values in enumerate([headers]+list(rows),1):
        cells=''.join(cell(f'{colname(ci)}{ri}',v,ri==1) for ci,v in enumerate(values,1))
        sheet_rows.append(f'<row r="{ri}">{cells}</row>')
    cols=''
    if widths:
        cols='<cols>'+''.join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i,w in enumerate(widths,1))+'</cols>'
    last=colname(len(headers)); lastrow=max(1,len(rows)+1)
    sheet=f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>{cols}<sheetData>{"".join(sheet_rows)}</sheetData><autoFilter ref="A1:{last}{lastrow}"/></worksheet>'
    workbook=f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{xml_escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    styles='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    files={
      '[Content_Types].xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
      '_rels/.rels':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
      'xl/workbook.xml':workbook,
      'xl/_rels/workbook.xml.rels':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
      'xl/worksheets/sheet1.xml':sheet,'xl/styles.xml':styles,
    }
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
        for name,content in files.items():z.writestr(name,content.encode('utf-8'))
    return stream.getvalue()
