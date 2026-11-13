from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sqlite3, json, secrets, hashlib, hmac, os, time, urllib.parse, mimetypes, zipfile, io, posixpath, re, uuid, unicodedata
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime, timedelta

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'idc_inventory.sqlite3'
HOST = os.environ.get('IDC_HOST', '127.0.0.1')
PORT = int(os.environ.get('IDC_PORT', '8002'))
SESSION_SECONDS = 12 * 60 * 60

IMPORT_TOKENS = {}
MOVEMENT_IMPORT_TOKENS = {}
SERVER_IMPORT_TOKENS = {}
SERVER_COMPONENT_IMPORT_TOKENS = {}
SERVER_SPARE_STATUSES = ('待检测','可用备件','待RMA','RMA处理中','RMA已完成','报废','其他')
SPARE_NOTES_LIMIT = 500  # 物料坏件与服务器备件统一口径
# 历史表用 server_id 关联（SN 可变）。-1 表示原服务器已删除、历史已归档，不会再被复用 SN 错挂。
ARCHIVED_SERVER_ID = -1
MATERIAL_SPARE_STATUSES = ('待处理','待RMA','RMA处理中','RMA已完成','可用备件','报废','其他')
IMPORT_LIMIT = 20 * 1024 * 1024
IMPORT_ROW_LIMIT = 10000
# 单个机房的物料量级为百万，留 10 倍余量；同时挡住超大整数导致的写库溢出。
MAX_QTY = 10_000_000
HEADER_SCAN_ROWS = 30
IMPORT_TOKEN_MAX = 60
ITEM_FIELDS = ('category','model','description','planned','supplier','location','owner','notes')
CALC_FIELDS = ('received','used','damaged')
FIELD_LABELS = {'category':'类别','model':'型号','description':'名称/规格','planned':'计划数量',
                'supplier':'供应商/品牌','location':'存放位置','owner':'负责人','notes':'备注'}
FIELD_LIMITS = {'model':64,'description':200,'supplier':100,'location':100,'owner':100,'notes':500}
# 计算列不参与字段映射竞争；过短的泛化别名（如「数量」）已从 planned 中移除，
# 避免把「未到数量」「到货数量」误判成计划数量。
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

def validate_item_fields(d, partial=False):
    """统一校验并规范化台账字段；partial=True 用于 PATCH。失败抛 ValueError。"""
    out={}
    for k in ITEM_FIELDS:
        if partial and k not in d:continue
        if k=='planned':
            try:
                raw=d.get('planned',0)
                if raw is None or str(raw).strip()=='' or isinstance(raw,bool):raise ValueError()
                q=float(raw)
                if not q.is_integer() or q<0:raise ValueError()
                out['planned']=int(q)
            except (ValueError,TypeError):raise ValueError('计划数量必须是大于等于 0 的整数')
        else:
            v=clean_text(d.get(k),k)
            if k in ('model','description') and not v:raise ValueError(f'{FIELD_LABELS[k]}不能为空')
            if k=='category' and v not in ('光模块','线缆'):raise ValueError('类别只能是光模块或线缆')
            out[k]=v
    if 'model' in out:out['model_key']=norm_model(out['model'])
    return out

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

def normalize_category(value):
    text=norm_header(value)
    if any(x in text for x in ('光模块','opticalmodule','transceiver')):return '光模块'
    if any(x in text for x in ('线缆','线材','cable','patchcord','网线')):return '线缆'
    if value in ('光模块','线缆'):return value
    return ''

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
    if missing:
        errors.append('缺少必需字段：'+ '、'.join(FIELD_LABELS.get(x,x) for x in dict.fromkeys(missing))
                      +'。可在下方「字段对应」中手动指定对应列，或改选表头所在行。')
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
    with db() as c:
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
                        q=float(str(raw).replace(',',''))
                        if q<0 or not q.is_integer():raise ValueError()
                        record['planned']=int(q)
                    except (ValueError,TypeError,AttributeError):
                        record['planned']=0; issues.append('计划数量必须是大于等于 0 的整数')
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
    if missing:
        errors.append('缺少必需字段：'+ '、'.join(MOVEMENT_LABELS.get(x,x) for x in missing)
                      +'。请在下方「字段对应」中手动指定对应列，或改选表头所在行。')
    mapped_cols={i for f,i in mapping.items() if f in MOVEMENT_FIELDS}
    unknown=[str(v) for i,v in enumerate(columns) if i not in mapped_cols and str(v).strip()]
    if unknown:warnings.append('未识别的列将忽略：'+ '、'.join(unknown[:12])+('…' if len(unknown)>12 else ''))
    if 'kind' not in mapping:warnings.append('表内未提供业务类型列，本批将统一按「入库」处理。')
    if 'date' not in mapping:warnings.append('表内未提供日期列，日期将使用导入当天。')
    if 'qty' in mapping and 0<=mapping['qty']<len(columns):
        qcol=str(columns[mapping['qty']] or '')
        if qcol and qcol.strip()!='入库数量' and ('计划' in qcol or 'planned' in norm_header(qcol)):
            warnings.append(f'数量列「{qcol}」含「计划」字样，请确认它确实是入库数量。')
    with db() as c:
        items={norm_model(r['model']):dict(r) for r in c.execute('SELECT id,category,model,description FROM items')}
    rows=[]; used_today=False
    for rn,row in [(rn,row) for rn,row in nonempty if rn>header_row]:
        if not any(str(v).strip() for v in row):continue
        def get(field):
            ix=mapping.get(field,-1)
            return str(row[ix] if 0<=ix<len(row) else '').strip()
        issues=[]; model=get('model'); key=norm_model(model)
        if not model:issues.append('型号为空')
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
        record={'item_id':base['id'] if base else None,
                'category':category,'model':base['model'] if base else model,
                'description':base['description'] if base else '',
                'kind':'入库','qty':qty,'moved_at':moved_at,
                'location':loc,'owner':own,'notes':nt,'row':rn}
        if issues:errors.append(f'第 {rn} 行：'+'；'.join(issues))
        rows.append(record)
    if used_today:warnings.append('缺失日期的记录将使用导入当天日期。')
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

# ===== GB300 导入：服务器清单 / 46U 部件（与物料台账、入库明细共用预览范式）=====
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
    best_rn = None; best_score = -1; best_map = {}; best_issues = []
    for rn, headers in candidates.items():
        found = {}; issues = []
        for ix, val in enumerate(headers):
            field, ambiguous = gb_header_match(val, spec['headers'])
            if ambiguous:
                issues.append('表头「%s」可能对应多个字段：' % val
                              + '、'.join(spec['labels'].get(x, x) for x in ambiguous))
            if field:
                if field in found:
                    issues.append('表头「%s」与前面的列冲突：都对应「%s」'
                                  % (val, spec['labels'].get(field, field)))
                else:
                    found[field] = ix
        score = len(found) + (8 if all(k in found for k in spec['required']) else 0)
        if score > best_score:
            best_rn, best_score, best_map, best_issues = rn, score, found, issues
    return best_rn, best_map, best_issues


def gb_context(kind):
    with db() as c:
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
        elif len(val) > 200:
            issues.append(label + '不能超过 200 个字符')
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


def build_gb_preview(batch, header_row, mapping, token, kind, header_issues=None):
    """自动识别与手动改映射共用同一套校验；纯空列自动隐藏，映射用原始列索引。"""
    spec = GB_SPECS[kind]
    nonempty = batch['nonempty']; candidates = batch['header_candidates']
    if header_row not in candidates:
        raise ValueError('请选择有效的表头所在行')
    columns = list(candidates[header_row])
    width = max([len(columns)] + [len(row) for _, row in nonempty])
    columns += [''] * (width - len(columns))
    active = active_column_indexes(columns, nonempty, header_row)
    errors = list(dict.fromkeys(header_issues or [])); warnings = []
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
    header_row, mapping, issues = auto_detect_gb_header(candidates, GB_SPECS[kind])
    token = secrets.token_urlsafe(24)
    batch = {'user_id': user_id, 'expires': int(time.time()) + 1200,
             'matrix': matrix, 'nonempty': nonempty,
             'header_candidates': candidates, 'header_row': header_row,
             'mapping': mapping, 'rows': [], 'columns': candidates[header_row],
             'kind': kind}
    if kind == 'components':
        batch['serials'] = []
    result = build_gb_preview(batch, header_row, mapping, token, kind,
                              header_issues=issues)
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

def db():
    c=sqlite3.connect(DB, timeout=15); c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); return c

def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, salt BLOB NOT NULL, pw_hash BLOB NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, can_view INTEGER NOT NULL DEFAULT 1, can_edit INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, csrf TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, category TEXT NOT NULL DEFAULT '光模块', model TEXT NOT NULL, model_key TEXT, description TEXT NOT NULL, supplier TEXT DEFAULT '', planned INTEGER NOT NULL DEFAULT 0, location TEXT DEFAULT '', owner TEXT DEFAULT '', notes TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(model));
        CREATE TABLE IF NOT EXISTS deletion_audit(id INTEGER PRIMARY KEY, table_name TEXT NOT NULL, record_id INTEGER, payload TEXT NOT NULL, deleted_by INTEGER, deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, restored_at TEXT, restored_by INTEGER);
        CREATE TABLE IF NOT EXISTS operation_log(id INTEGER PRIMARY KEY, action TEXT NOT NULL, target TEXT DEFAULT '', detail TEXT DEFAULT '', operated_by INTEGER, operated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS movements(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), kind TEXT NOT NULL CHECK(kind IN ('入库','领用/安装','报废')), qty INTEGER NOT NULL CHECK(qty>0), moved_at TEXT NOT NULL, location TEXT DEFAULT '', owner TEXT DEFAULT '', handler TEXT DEFAULT '', reference TEXT DEFAULT '', notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS servers(id INTEGER PRIMARY KEY, room TEXT NOT NULL, rack TEXT NOT NULL, sn TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS server_components(id INTEGER PRIMARY KEY, server_sn TEXT NOT NULL REFERENCES servers(sn) ON UPDATE CASCADE ON DELETE CASCADE, node_sn TEXT NOT NULL, node_u INTEGER NOT NULL CHECK(node_u BETWEEN 1 AND 46), kp_name TEXT NOT NULL, bmc_mac TEXT DEFAULT '', UNIQUE(server_sn,node_u), UNIQUE(server_sn,node_sn));
        CREATE TABLE IF NOT EXISTS server_component_operations(id INTEGER PRIMARY KEY, server_sn TEXT NOT NULL, node_u INTEGER NOT NULL, action TEXT NOT NULL, responsible TEXT NOT NULL, old_data TEXT NOT NULL, new_data TEXT NOT NULL, operated_by INTEGER REFERENCES users(id), operated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS server_spares(id INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL UNIQUE REFERENCES server_component_operations(id), server_sn TEXT NOT NULL, node_sn TEXT NOT NULL, node_u INTEGER NOT NULL, kp_name TEXT NOT NULL, bmc_mac TEXT DEFAULT '', room TEXT DEFAULT '', rack TEXT DEFAULT '', status TEXT NOT NULL DEFAULT '待检测', responsible TEXT NOT NULL, notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS server_spare_status_history(id INTEGER PRIMARY KEY, spare_id INTEGER NOT NULL REFERENCES server_spares(id) ON DELETE CASCADE, from_status TEXT NOT NULL, to_status TEXT NOT NULL, responsible TEXT NOT NULL, notes TEXT DEFAULT '', operated_by INTEGER REFERENCES users(id), operated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_server_spares_status ON server_spares(status,created_at);
        CREATE TABLE IF NOT EXISTS material_spare_cases(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, qty INTEGER NOT NULL CHECK(qty>0), returned_qty INTEGER NOT NULL DEFAULT 0 CHECK(returned_qty>=0), scrapped_qty INTEGER NOT NULL DEFAULT 0 CHECK(scrapped_qty>=0), status TEXT NOT NULL DEFAULT '待RMA', responsible TEXT NOT NULL, notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS material_spare_case_history(id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES material_spare_cases(id) ON DELETE CASCADE, from_status TEXT NOT NULL, to_status TEXT NOT NULL, qty_returned INTEGER NOT NULL DEFAULT 0, qty_scrapped INTEGER NOT NULL DEFAULT 0, responsible TEXT NOT NULL, notes TEXT DEFAULT '', operated_by INTEGER REFERENCES users(id), operated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_material_spare_cases_item_status ON material_spare_cases(item_id,status);
        CREATE TABLE IF NOT EXISTS material_spares(id INTEGER PRIMARY KEY, movement_id INTEGER REFERENCES movements(id) ON DELETE CASCADE, unit_index INTEGER NOT NULL DEFAULT 1, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, serial_number TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '待处理', responsible TEXT DEFAULT '', notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(movement_id,unit_index));
        CREATE TABLE IF NOT EXISTS material_spare_history(id INTEGER PRIMARY KEY, spare_id INTEGER NOT NULL REFERENCES material_spares(id) ON DELETE CASCADE, from_status TEXT NOT NULL, to_status TEXT NOT NULL, old_serial_number TEXT NOT NULL DEFAULT '', serial_number TEXT NOT NULL DEFAULT '', responsible TEXT DEFAULT '', notes TEXT DEFAULT '', operated_by INTEGER REFERENCES users(id), operated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_material_spares_status ON material_spares(status,created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_material_spares_item_sn ON material_spares(item_id,serial_number) WHERE serial_number<>'';
        CREATE INDEX IF NOT EXISTS idx_movements_item ON movements(item_id,kind);
        CREATE INDEX IF NOT EXISTS idx_servers_room_rack ON servers(room,rack);
        CREATE INDEX IF NOT EXISTS idx_server_components_sn ON server_components(server_sn,node_u);
        ''')
        # 历史表改用 server_id：只回填一次，匹配不到服务器的记录直接标记为已归档(-1)，
        # 之后即使用同一个 SN 新建服务器，也不会被错误关联回新机器。
        for _t in ('server_component_operations','server_spares'):
            _cols={r['name'] for r in c.execute(f'PRAGMA table_info({_t})')}
            if 'server_id' not in _cols:
                c.execute(f'ALTER TABLE {_t} ADD COLUMN server_id INTEGER')
            c.execute(f'''UPDATE {_t} SET server_id=COALESCE(
                (SELECT id FROM servers WHERE servers.sn={_t}.server_sn), -1)
                WHERE server_id IS NULL''')
        audit_cols={r['name'] for r in c.execute('PRAGMA table_info(deletion_audit)')}
        for col,decl in (('restored_at','TEXT'),('restored_by','INTEGER')):
            if col not in audit_cols:c.execute(f'ALTER TABLE deletion_audit ADD COLUMN {col} {decl}')
        cols={r['name'] for r in c.execute('PRAGMA table_info(items)')}
        if 'category' not in cols: c.execute("ALTER TABLE items ADD COLUMN category TEXT NOT NULL DEFAULT '光模块'")
        # 型号归一化唯一键：全角/半角、大小写、首尾空格视为同一型号。
        if 'model_key' not in cols: c.execute('ALTER TABLE items ADD COLUMN model_key TEXT')
        for row in c.execute("SELECT id,model FROM items WHERE model_key IS NULL OR model_key=''").fetchall():
            c.execute('UPDATE items SET model_key=? WHERE id=?',(norm_model(row['model']),row['id']))
        dup=c.execute('SELECT model_key,COUNT(*) n FROM items GROUP BY model_key HAVING n>1').fetchall()
        if not dup:c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_items_model_key ON items(model_key)')
        else:print('警告：存在归一化后重复的型号，未建立 model_key 唯一索引：',[r['model_key'] for r in dup])
        movement_cols={r['name'] for r in c.execute('PRAGMA table_info(movements)')}
        if 'owner' not in movement_cols:
            c.execute("ALTER TABLE movements ADD COLUMN owner TEXT DEFAULT ''")
            c.execute("UPDATE movements SET owner=COALESCE(handler,'')")
        # Backfill legacy scrap transactions once. Each scrapped unit becomes an
        # individual pending spare record whose serial number can be entered later.
        for movement in c.execute("SELECT id,item_id,qty,created_by FROM movements WHERE kind='报废'").fetchall():
            for unit in range(1,movement['qty']+1):
                c.execute("INSERT OR IGNORE INTO material_spares(movement_id,unit_index,item_id,created_by) VALUES(?,?,?,?)",(movement['id'],unit,movement['item_id'],movement['created_by']))
        # Recover older recorded part replacements that predate automatic spare
        # registration. The operation log already contains the removed NODE_SN.
        missing_replacements=c.execute('''SELECT o.* FROM server_component_operations o
            LEFT JOIN server_spares s ON s.operation_id=o.id
            WHERE o.action='部件更换' AND s.id IS NULL AND o.server_id>0 ORDER BY o.id''').fetchall()
        for operation in missing_replacements:
            try:
                old_data=json.loads(operation['old_data']);new_data=json.loads(operation['new_data'])
                old_sn=str(old_data.get('node_sn','')).strip();new_sn=str(new_data.get('node_sn','')).strip()
                node_u=int(old_data.get('node_u',operation['node_u']))
                if not old_sn or old_sn==new_sn:continue
                place=c.execute('SELECT room,rack FROM servers WHERE sn=?',(operation['server_sn'],)).fetchone()
                room,rack=(place['room'],place['rack']) if place else ('','')
                spare=c.execute('''INSERT OR IGNORE INTO server_spares(operation_id,server_sn,server_id,node_sn,node_u,kp_name,bmc_mac,room,rack,status,responsible,created_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(operation['id'],operation['server_sn'],operation['server_id'],old_sn,node_u,str(old_data.get('kp_name','')),str(old_data.get('bmc_mac','')),room,rack,'待检测',operation['responsible'],operation['operated_by']))
                if spare.lastrowid:
                    c.execute('''INSERT INTO server_spare_status_history(spare_id,from_status,to_status,responsible,notes,operated_by)
                        VALUES(?,?,?,?,?,?)''',(spare.lastrowid,'历史更换记录','待检测',operation['responsible'],'根据既有部件更换操作记录补入服务器备件',operation['operated_by']))
            except (TypeError,ValueError,json.JSONDecodeError):continue
        duplicates=c.execute('SELECT model FROM items GROUP BY model HAVING COUNT(*)>1').fetchall()
        if not duplicates:c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_items_model_unique ON items(model)')

def pw_hash(password,salt): return hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),salt,310000)
def json_bytes(obj): return json.dumps(obj,ensure_ascii=False).encode('utf-8')

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

def stock_available(c,item_id):
    r=c.execute('''SELECT COALESCE(SUM(CASE WHEN kind='入库' THEN qty ELSE 0 END),0)
                          -COALESCE(SUM(CASE WHEN kind='领用/安装' THEN qty ELSE 0 END),0)
                          -COALESCE(SUM(CASE WHEN kind='报废' THEN qty ELSE 0 END),0) n
                   FROM movements WHERE item_id=?''',(item_id,)).fetchone()
    return r['n'] if r else 0

def assert_stock(c,item_ids,action='该操作'):
    """事务内调用：任一受影响物料出现负库存就抛错，由外层回滚整笔操作。"""
    for iid in {int(x) for x in item_ids if x is not None}:
        n=stock_available(c,iid)
        if n<0:
            raise ValueError(f'可用库存不足：{action}会使库存变为 {n}，不允许负库存，本次操作已撤销')

def predict_negative(c,movement_id,new_item_id,new_kind,new_qty):
    """编辑明细前预演：把旧明细从汇总里剔除、加上新明细，是否会出现负库存。"""
    old=c.execute('SELECT item_id,kind,qty FROM movements WHERE id=?',(movement_id,)).fetchone()
    if not old:return False
    def delta(k,q):return q if k=='入库' else -q
    for iid in {old['item_id'],new_item_id}:
        total=stock_available(c,iid)
        if old['item_id']==iid:total-=delta(old['kind'],old['qty'])
        if iid==new_item_id:total+=delta(new_kind,new_qty)
        if total<0:return True
    return False

def to_local(ts):
    """SQLite 的 CURRENT_TIMESTAMP 是 UTC，展示时转成本地时间。"""
    try:
        utc=datetime.strptime(str(ts),'%Y-%m-%d %H:%M:%S')
        return (utc+timedelta(seconds=-time.timezone)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(ts or '')

def log_action(c,user_id,action,target='',detail=''):
    """在已有连接/事务里追加一条操作日志。"""
    c.execute('INSERT INTO operation_log(action,target,detail,operated_by) VALUES(?,?,?,?)',
              (action,str(target or '')[:200],str(detail or '')[:500],user_id))

def snapshot_plan(data):
    """按外键依赖顺序返回 (表名, 记录列表)，用于冲突检测与还原。"""
    item=data.get('item') or {}
    return [('items',[item] if item else []),
            ('movements',data.get('movements') or []),
            ('material_spare_cases',data.get('material_spare_cases') or []),
            ('material_spares',data.get('material_spares') or []),
            ('material_spare_history',data.get('material_spare_history') or []),
            ('material_spare_case_history',data.get('material_spare_case_history') or [])]

def restore_snapshot(c,row,user_id):
    """把删除前快照原样写回；任何冲突抛 ValueError，由调用方用 SAVEPOINT 回滚该条。"""
    if row['table_name']!='items':raise ValueError('该类型暂不支持还原')
    try:data=json.loads(row['payload'])
    except Exception:raise ValueError('快照内容无法解析')
    if not isinstance(data,dict):raise ValueError('快照格式无效')
    item=data.get('item') or {}
    if not item.get('id'):raise ValueError('快照缺少物料记录')
    plan=snapshot_plan(data)
    labels={'items':'物料台账','movements':'库存变动明细','material_spare_cases':'备件单',
            'material_spares':'坏件记录','material_spare_history':'坏件状态历史',
            'material_spare_case_history':'备件单历史'}
    for table,recs in plan:
        cols={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
        for rec in recs:
            if rec.get('id') is None or 'id' not in cols:continue
            if c.execute(f'SELECT 1 FROM {table} WHERE id=?',(rec['id'],)).fetchone():
                raise ValueError(f'{labels.get(table,table)}的原编号 {rec["id"]} 已被占用，无法原样还原')
    key=norm_model(item.get('model'))
    if c.execute('SELECT 1 FROM items WHERE model_key=? OR model=?',(key,item.get('model'))).fetchone():
        raise ValueError(f'型号「{item.get("model")}」已存在于台账，无法还原')
    for table,recs in plan:
        cols={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
        for rec in recs:
            keys=[k for k in rec if k in cols]
            if not keys:continue
            sql=f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' for _ in keys)})"
            c.execute(sql,[rec[k] for k in keys])
    c.execute('UPDATE deletion_audit SET restored_at=CURRENT_TIMESTAMP,restored_by=? WHERE id=?',(user_id,row['id']))
    return item.get('model','')
class Handler(BaseHTTPRequestHandler):
    server_version='IDCInventory/1.0'
    def log_message(self,fmt,*args): print('%s - %s' % (self.address_string(),fmt%args))
    def handle_one_request(self):
        try:super().handle_one_request()
        except (BrokenPipeError,ConnectionResetError):self.close_connection=True
        except Exception as e:
            print('[unhandled] %s: %s'%(type(e).__name__,e))
            try:self.send(500,{'error':'服务器内部错误，请稍后重试'})
            except Exception:self.close_connection=True
    def do_PUT(self): self.send(405,{'error':'不支持的请求方法；新增用 POST，修改用 PATCH，删除用 DELETE'})
    def send_error(self,code,message=None,explain=None):
        self.close_connection=True
        try:self.send(code,{'error':message or '请求无法处理'})
        except Exception:pass
    def send(self,status,obj=None,headers=None,content_type='application/json; charset=utf-8'):
        body=obj if isinstance(obj,bytes) else json_bytes(obj or {})
        self.send_response(status); self.send_header('Content-Type',content_type); self.send_header('Content-Length',str(len(body))); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Cache-Control','no-store')
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)
    def body(self):
        n=int(self.headers.get('Content-Length','0') or 0)
        if n>1_000_000: raise ValueError('请求内容过大')
        try: d=json.loads(self.rfile.read(n) or b'{}')
        except ValueError:raise ValueError('请求内容不是有效的 JSON')
        if not isinstance(d,dict):raise ValueError('请求内容必须是 JSON 对象')
        return d
    def auth(self,csrf=False):
        token=''
        for part in self.headers.get('Cookie','').split(';'):
            if part.strip().startswith('sid='): token=part.strip()[4:]
        if not token: return None
        th=hashlib.sha256(token.encode()).hexdigest()
        with db() as c:
            row=c.execute('SELECT s.csrf,s.expires,u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND u.active=1',(th,)).fetchone()
            if not row or row['expires']<int(time.time()):
                c.execute('DELETE FROM sessions WHERE token_hash=?',(th,)); return None
            if csrf and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),row['csrf']): return False
            return dict(row)
    def require(self,perm=None,csrf=True):
        u=self.auth(csrf)
        if u is None: self.send(401,{'error':'请先登录'}); return None
        if u is False: self.send(403,{'error':'请求校验失败，请刷新页面后重试'}); return None
        if perm and not u['is_admin'] and not u[perm]: self.send(403,{'error':'当前账号没有此操作权限'}); return None
        return u
    def do_GET(self):
        p=urllib.parse.urlparse(self.path).path
        if p=='/':
            try: body=(ROOT/'index.html').read_bytes()
            except OSError: return self.send(500,{'error':'页面文件缺失'})
            return self.send(200,body,content_type='text/html; charset=utf-8')
        if p=='/api/export/items.xlsx':
            u=self.require('can_view',False)
            if not u:return
            with db() as c:
                rows=c.execute('''SELECT i.category,i.model,i.description,i.supplier,i.planned,
                COALESCE(SUM(CASE WHEN m.kind='入库' THEN m.qty ELSE 0 END),0) received,
                COALESCE(SUM(CASE WHEN m.kind='领用/安装' THEN m.qty ELSE 0 END),0) used,
                COALESCE(SUM(CASE WHEN m.kind='报废' THEN m.qty ELSE 0 END),0) damaged,
                i.location,i.owner,i.notes FROM items i LEFT JOIN movements m ON m.item_id=i.id GROUP BY i.id ORDER BY i.id''').fetchall()
            data=[]
            for r in rows:
                d=dict(r);d['available']=d['received']-d['used']-d['damaged'];d['not_received']=max(d['planned']-d['received'],0)
                data.append([d['category'],d['model'],d['description'],d['supplier'],d['planned'],d['received'],d['used'],d['damaged'],d['available'],d['not_received'],d['location'],d['owner'],d['notes']])
            headers=['类别','型号','名称/规格','供应商/品牌','计划数量','累计入库','已领用/安装','损坏/报废','可用库存','未到数量','存放位置','负责人','备注']
            payload=make_xlsx('物料台账',headers,data,[14,20,30,22,12,12,14,14,12,12,20,16,28])
            return self.send(200,payload,headers={'Content-Disposition':'attachment; filename=IDC_material_ledger.xlsx'},content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        if p=='/api/export/movements.xlsx':
            u=self.require('can_view',False)
            if not u:return
            with db() as c: rows=c.execute('SELECT m.moved_at,m.kind,i.category,i.model,m.qty,m.location,m.owner,m.notes FROM movements m JOIN items i ON i.id=m.item_id ORDER BY m.moved_at,m.id').fetchall()
            headers=['日期','业务类型','类别','型号','数量','位置/去向','负责人','备注']
            payload=make_xlsx('库存变动明细',headers,[list(r) for r in rows],[14,16,14,20,12,24,18,32])
            return self.send(200,payload,headers={'Content-Disposition':'attachment; filename=IDC_inventory_movements.xlsx'},content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        if p=='/api/status':
            with db() as c: setup=c.execute('SELECT count(*) FROM users').fetchone()[0]==0
            return self.send(200,{'setup_required':setup})
        if p=='/api/config':
            # 开放化第一步：把目前写死在后端/前端的业务口径集中到一个只读接口。
            # 后续改成可配置字典时，只要本接口返回值不变，前端与导入逻辑都无需修改。
            return self.send(200,{
                'item_categories':['光模块','线缆'],
                'item_fields':list(ITEM_FIELDS),'field_limits':dict(FIELD_LIMITS),
                'material_spare_statuses':list(MATERIAL_SPARE_STATUSES),
                'server_spare_statuses':list(SERVER_SPARE_STATUSES),
                'rack_u_slots':46,'qty_max':MAX_QTY,
                'import':{'header_scan_rows':HEADER_SCAN_ROWS,
                          'row_limit':IMPORT_ROW_LIMIT,'page_size':50},
                'configurable':False})
        if p=='/api/me':
            u=self.auth()
            if not u: return self.send(401,{'error':'请先登录'})
            return self.send(200,{'id':u['id'],'username':u['username'],'is_admin':bool(u['is_admin']),'can_view':bool(u['can_view']),'can_edit':bool(u['can_edit']),'csrf':u['csrf']})
        if p=='/api/items':
            u=self.require('can_view',False)
            if not u:return
            with db() as c:
                rows=c.execute('''SELECT i.*,
                COALESCE(SUM(CASE WHEN m.kind='入库' THEN m.qty ELSE 0 END),0) received,
                COALESCE(SUM(CASE WHEN m.kind='领用/安装' THEN m.qty ELSE 0 END),0) used,
                COALESCE(SUM(CASE WHEN m.kind='报废' THEN m.qty ELSE 0 END),0) damaged
                FROM items i LEFT JOIN movements m ON m.item_id=i.id GROUP BY i.id ORDER BY i.id DESC''').fetchall()
            out=[]
            for x in rows:
                d=dict(x); d['available']=d['received']-d['used']-d['damaged']; d['not_received']=max(d['planned']-d['received'],0); out.append(d)
            return self.send(200,{'items':out})
        if p=='/api/spares':
            u=self.require('can_view',False)
            if not u:return
            with db() as c:
                rows=c.execute('''SELECT i.id,i.category,i.model,i.description,
                    COALESCE(m.received,0) received,COALESCE(m.used,0) used,COALESCE(m.damaged,0) damaged,
                    COALESCE(r.waiting,0) rma_waiting,COALESCE(r.processing,0) rma_processing
                    FROM items i
                    LEFT JOIN (SELECT item_id,SUM(CASE WHEN kind='入库' THEN qty ELSE 0 END) received,
                        SUM(CASE WHEN kind='领用/安装' THEN qty ELSE 0 END) used,
                        SUM(CASE WHEN kind='报废' THEN qty ELSE 0 END) damaged FROM movements GROUP BY item_id) m ON m.item_id=i.id
                    LEFT JOIN (SELECT item_id,
                        SUM(CASE WHEN status='待RMA' THEN 1 ELSE 0 END) waiting,
                        SUM(CASE WHEN status='RMA处理中' THEN 1 ELSE 0 END) processing
                        FROM material_spares GROUP BY item_id) r ON r.item_id=i.id
                    WHERE i.category IN ('光模块','线缆') ORDER BY i.category,i.model''').fetchall()
                materials=[]
                for row in rows:
                    d=dict(row);d['ledger_available']=d['received']-d['used']-d['damaged'];d['rma_hold']=0;d['available']=d['ledger_available']
                    if d['available']>0 or d['damaged']>0:materials.append(d)
                material_spares=[dict(r) for r in c.execute('''SELECT s.*,i.category,i.model,i.description,m.moved_at,m.location AS source_location,m.owner AS source_owner,
                    u.username AS operator_username FROM material_spares s JOIN items i ON i.id=s.item_id
                    LEFT JOIN movements m ON m.id=s.movement_id LEFT JOIN users u ON u.id=s.created_by
                    ORDER BY CASE WHEN s.status='待处理' THEN 0 ELSE 1 END,s.created_at DESC,s.id DESC''').fetchall()]
                material_history=c.execute('''SELECT h.*,u.username AS operator_username FROM material_spare_history h
                    LEFT JOIN users u ON u.id=h.operated_by ORDER BY h.id DESC''').fetchall()
                server_spares=[dict(r) for r in c.execute('''SELECT s.*,u.username AS operator_username,
                    sv.sn AS current_sn,
                    CASE WHEN s.server_id IS NULL OR s.server_id=? OR sv.id IS NULL THEN 1 ELSE 0 END AS orphan
                    FROM server_spares s LEFT JOIN users u ON u.id=s.created_by
                    LEFT JOIN servers sv ON sv.id=s.server_id
                    ORDER BY s.created_at DESC,s.id DESC''',(ARCHIVED_SERVER_ID,)).fetchall()]
                history=c.execute('''SELECT h.*,u.username AS operator_username FROM server_spare_status_history h
                    LEFT JOIN users u ON u.id=h.operated_by ORDER BY h.id DESC''').fetchall()
            material_history_by_spare={}
            for h in material_history:material_history_by_spare.setdefault(h['spare_id'],[]).append(dict(h))
            for spare in material_spares:spare['history']=material_history_by_spare.get(spare['id'],[])
            history_by_spare={}
            for h in history:history_by_spare.setdefault(h['spare_id'],[]).append(dict(h))
            for spare in server_spares:spare['history']=history_by_spare.get(spare['id'],[])
            return self.send(200,{'materials':materials,'material_spares':material_spares,'material_statuses':MATERIAL_SPARE_STATUSES,'server_spares':server_spares,'statuses':SERVER_SPARE_STATUSES})
        if p=='/api/movements':
            u=self.require('can_view',False)
            if not u:return
            with db() as c: rows=c.execute('SELECT m.id,m.moved_at,m.kind,i.category,i.model,m.qty,m.location,m.owner,m.notes FROM movements m JOIN items i ON i.id=m.item_id ORDER BY m.moved_at DESC,m.id DESC LIMIT 1000').fetchall()
            return self.send(200,{'movements':[dict(x) for x in rows]})
        if p=='/api/servers':
            u=self.require('can_view',False)
            if not u:return
            with db() as c: rows=c.execute('SELECT id,room,rack,sn FROM servers ORDER BY room,rack,sn').fetchall()
            return self.send(200,{'servers':[dict(x) for x in rows]})
        if p.startswith('/api/servers/') and p.endswith('/components'):
            u=self.require('can_view',False)
            if not u:return
            try:server_id=int(p.split('/')[3])
            except:return self.send(400,{'error':'服务器编号无效'})
            with db() as c:
                server=c.execute('SELECT id,room,rack,sn FROM servers WHERE id=?',(server_id,)).fetchone()
                if not server:return self.send(404,{'error':'服务器不存在'})
                components=c.execute('SELECT id,node_sn,node_u,kp_name,bmc_mac FROM server_components WHERE server_sn=? ORDER BY node_u DESC',(server['sn'],)).fetchall()
                operations=c.execute('''SELECT o.id,o.node_u,o.action,o.responsible,o.old_data,o.new_data,o.operated_at,u.username AS operator_username
                    FROM server_component_operations o LEFT JOIN users u ON u.id=o.operated_by
                    WHERE o.server_id=? ORDER BY o.id DESC''',(server['id'],)).fetchall()
            return self.send(200,{'server':dict(server),'components':[dict(x) for x in components],'operations':[dict(x) for x in operations]})
        if p=='/api/recycle' or p.startswith('/api/recycle/'):
            u=self.require(csrf=False)
            if not u:return
            if not u['is_admin']:return self.send(403,{'error':'仅管理员可查看回收站'})
            detail_id=None
            if p!='/api/recycle':
                try:detail_id=int(p.rsplit('/',1)[1])
                except:return self.send(400,{'error':'记录编号无效'})
            with db() as c:
                if detail_id is not None:
                    row=c.execute('SELECT * FROM deletion_audit WHERE id=?',(detail_id,)).fetchone()
                    if not row:return self.send(404,{'error':'回收站记录不存在'})
                    try:snap=json.loads(row['payload'])
                    except Exception:snap={}
                    return self.send(200,{'id':row['id'],'deleted_at':to_local(row['deleted_at']),
                                          'restored_at':to_local(row['restored_at']) if row['restored_at'] else None,
                                          'snapshot':snap})
                rows=[dict(r) for r in c.execute('''SELECT a.*,u.username AS operator FROM deletion_audit a
                    LEFT JOIN users u ON u.id=a.deleted_by ORDER BY a.id DESC LIMIT 500''').fetchall()]
                occupied={r['id'] for r in c.execute('SELECT id FROM items')}
                occupied_keys={r['model_key'] for r in c.execute('SELECT model_key FROM items')}
                records=[]
                for r in rows:
                    try:data=json.loads(r['payload'])
                    except Exception:data={}
                    data=data if isinstance(data,dict) else {}
                    it=data.get('item') or {}
                    key=norm_model(it.get('model'))
                    reason=''
                    if r['restored_at']:reason='已还原'
                    elif r['table_name']!='items':reason='该类型暂不支持还原'
                    elif it.get('id') in occupied:reason='原物料编号已被占用'
                    elif key in occupied_keys:reason='型号已被占用'
                    records.append({'id':r['id'],'table_name':r['table_name'],
                        'record_id':r['record_id'],'deleted_at':to_local(r['deleted_at']),
                        'restored_at':to_local(r['restored_at']) if r['restored_at'] else None,
                        'operator':r['operator'],
                        'category':it.get('category',''),'model':it.get('model',''),
                        'description':it.get('description',''),
                        'movement_count':len(data.get('movements') or []),
                        'spare_count':len(data.get('material_spares') or []),
                        'can_restore':not reason,'reason':reason,
                        'snapshot':data})
                logs=[]
                for r in c.execute('''SELECT l.*,u.username AS operator FROM operation_log l
                    LEFT JOIN users u ON u.id=l.operated_by ORDER BY l.id DESC LIMIT 300''').fetchall():
                    x=dict(r);x['operated_at']=to_local(x.get('operated_at'));logs.append(x)
            return self.send(200,{'records':records,'logs':logs})
        if p=='/api/users':
            u=self.require(csrf=False)
            if not u:return
            if not u['is_admin']: return self.send(403,{'error':'仅管理员可管理账号'})
            with db() as c: rows=c.execute('SELECT id,username,is_admin,can_view,can_edit,active,created_at FROM users ORDER BY id').fetchall()
            return self.send(200,{'users':[dict(x) for x in rows]})
        return self.send(404,{'error':'没有这个页面'})
    def do_POST(self):
        p=urllib.parse.urlparse(self.path).path
        if p=='/api/material-spare-cases':return self.send(410,{'error':'备件由库存变动明细中的报废记录自动生成'})
        if p=='/api/import-server-components-preview':
            u=self.require('can_edit')
            if not u:return
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>IMPORT_LIMIT:return self.send(400,{'error':'请选择有效的 .xlsx 文件（最大 20 MB）'})
            try:result=parse_gb_import(self.rfile.read(n),u['id'],'components')
            except ValueError as e:return self.send(400,{'error':str(e)})
            except Exception:return self.send(400,{'error':'Excel 文件结构无法读取，请另存为标准 .xlsx 文件后重试'})
            return self.send(200,result)
        if p=='/api/import-server-components':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取导入确认请求'})
            token=str(d.get('token',''));batch=SERVER_COMPONENT_IMPORT_TOKENS.pop(token,None)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):return self.send(400,{'error':'部件清单预览已失效，请重新上传文件'})
            edited=d.get('rows')
            if edited is not None:
                try:
                    if not isinstance(edited,list) or len(edited)!=len(batch['rows']):
                        raise ValueError('编辑后的行数与预览不一致，请重新上传')
                    ctx=gb_context('components');clean=[]
                    for i,raw in enumerate(edited,1):
                        vals={'server_sn':str(raw.get('server_sn','')).strip(),
                              'node_sn':str(raw.get('node_sn','')).strip(),
                              'node':str(raw.get('node_u','')).strip(),
                              'kp_name':str(raw.get('kp_name','')).strip(),
                              'bmc_mac':str(raw.get('bmc_mac','')).strip()}
                        rec,issues=parse_component_row(lambda f:vals.get(f,''),i,ctx)
                        if issues:raise ValueError(f'第 {i} 行：'+'；'.join(issues))
                        clean.append(rec)
                    batch['rows']=clean;batch['serials']=sorted(ctx['serials'])
                except ValueError as e:return self.send(400,{'error':str(e)})
            try:
                with db() as c:
                    current={r['sn'] for r in c.execute('SELECT sn FROM servers')}
                    if not set(batch['serials']).issubset(current):raise ValueError('服务器台账中的 SN 已变更，请重新预览文件')
                    for server_sn in batch['serials']:c.execute('DELETE FROM server_components WHERE server_sn=?',(server_sn,))
                    c.executemany('INSERT INTO server_components(server_sn,node_sn,node_u,kp_name,bmc_mac) VALUES(?,?,?,?,?)',[(x['server_sn'],x['node_sn'],x['node_u'],x['kp_name'],x['bmc_mac']) for x in batch['rows']])
            except ValueError as e:return self.send(409,{'error':str(e)})
            except sqlite3.IntegrityError:return self.send(409,{'error':'部件数据存在重复 NODE_SN 或 NODE，整批未导入；请重新预览'})
            return self.send(200,{'ok':True,'imported':len(batch['rows']),'servers':len(batch['serials'])})
        if p=='/api/import-servers-preview':
            u=self.require('can_edit')
            if not u:return
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>IMPORT_LIMIT:return self.send(400,{'error':'请选择有效的 .xlsx 文件（最大 20 MB）'})
            try:result=parse_gb_import(self.rfile.read(n),u['id'],'servers')
            except ValueError as e:return self.send(400,{'error':str(e)})
            except Exception:return self.send(400,{'error':'Excel 文件结构无法读取，请另存为标准 .xlsx 文件后重试'})
            return self.send(200,result)
        if p=='/api/import-servers':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取导入确认请求'})
            token=str(d.get('token',''));batch=SERVER_IMPORT_TOKENS.pop(token,None)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):return self.send(400,{'error':'预览已失效，请重新上传文件'})
            edited=d.get('rows')
            if edited is not None:
                try:
                    if not isinstance(edited,list) or len(edited)!=len(batch['rows']):
                        raise ValueError('编辑后的行数与预览不一致，请重新上传')
                    ctx=gb_context('servers');clean=[]
                    for i,raw in enumerate(edited,1):
                        vals={f:str(raw.get(f,'')).strip() for f in SERVER_FIELDS}
                        rec,issues=parse_server_row(lambda f:vals.get(f,''),i,ctx)
                        if issues:raise ValueError(f'第 {i} 行：'+'；'.join(issues))
                        clean.append(rec)
                    batch['rows']=clean
                except ValueError as e:return self.send(400,{'error':str(e)})
            try:
                with db() as c:
                    for server in batch['rows']:
                        if c.execute('SELECT 1 FROM servers WHERE sn=?',(server['sn'],)).fetchone():raise ValueError('确认导入前发现重复 SN，请重新上传并处理重复记录')
                        c.execute('INSERT INTO servers(room,rack,sn) VALUES(?,?,?)',(server['room'],server['rack'],server['sn']))
            except ValueError as e:return self.send(409,{'error':str(e)})
            except sqlite3.IntegrityError:return self.send(409,{'error':'导入期间发现重复 SN，整批未写入；请重新预览文件'})
            return self.send(200,{'ok':True,'imported':len(batch['rows'])})

        if p in ('/api/import-servers-remap','/api/import-server-components-remap'):
            u=self.require('can_edit')
            if not u:return
            kind='servers' if p=='/api/import-servers-remap' else 'components'
            store=SERVER_IMPORT_TOKENS if kind=='servers' else SERVER_COMPONENT_IMPORT_TOKENS
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取字段映射请求'})
            token=str(d.get('edit_token',''));batch=store.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):
                return self.send(400,{'error':'预览已失效，请重新上传 Excel 文件'})
            try:
                header_row=int(d.get('header_row'))
                raw=d.get('mapping_index') or {}
                if not isinstance(raw,dict):raise ValueError('字段映射格式不正确')
                spec=GB_SPECS[kind];mapping={};used=set()
                for field,col in raw.items():
                    if field not in spec['fields']:continue
                    ix=int(col)
                    if ix<0 or ix>=len(batch['columns']):raise ValueError(f'列序号 {ix} 超出表格范围')
                    if ix in used:raise ValueError('同一列不能同时对应多个字段')
                    used.add(ix);mapping[field]=ix
                result=remap_gb_import(batch,header_row,mapping,token)
            except (ValueError,TypeError,KeyError) as e:
                return self.send(400,{'error':f'字段映射无效：{e}'})
            return self.send(200,result)

        if p=='/api/import-preview':
            u=self.require('can_edit')
            if not u:return
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>IMPORT_LIMIT:return self.send(400,{'error':'请选择有效的 .xlsx 文件（最大 20 MB）'})
            raw=self.rfile.read(n)
            default_category={'optical':'光模块','cable':'线缆'}.get(self.headers.get('X-Category-Default',''),self.headers.get('X-Category-Default',''))
            mode=self.headers.get('X-Import-Mode','create')
            if mode not in ('create','update'):mode='create'
            try: result=parse_item_import(raw,default_category,u['id'],mode)
            except ValueError as e:return self.send(400,{'error':str(e)})
            except Exception:return self.send(400,{'error':'Excel 文件结构无法读取，请另存为标准 .xlsx 文件后重试'})
            return self.send(200,result)
        if p=='/api/import-remap':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取字段映射请求'})
            token=str(d.get('edit_token',''))
            batch=IMPORT_TOKENS.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):
                return self.send(400,{'error':'预览已失效，请重新上传 Excel 文件'})
            try:
                header_row=int(d.get('header_row'))
                raw=d.get('mapping_index') or {}
                if not isinstance(raw,dict):raise ValueError('字段映射格式不正确')
                mapping={}; used=set()
                for field,col in raw.items():
                    if field not in ITEM_FIELDS and field not in CALC_FIELDS:continue
                    ix=int(col)
                    if ix<0 or ix>=len(batch['columns']):raise ValueError(f'列序号 {ix} 超出表格范围')
                    if ix in used:raise ValueError('同一列不能同时对应多个字段')
                    used.add(ix); mapping[field]=ix
                result=remap_item_import(batch,header_row,mapping,token)
            except (ValueError,TypeError,KeyError) as e:
                return self.send(400,{'error':f'字段映射无效：{e}'})
            return self.send(200,result)
        if p=='/api/recycle/restore':
            u=self.require('can_edit')
            if not u:return
            if not u['is_admin']:return self.send(403,{'error':'仅管理员可还原记录'})
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取还原请求'})
            with db() as c:
                ids=d.get('ids')
                if d.get('all'):
                    ids=[r['id'] for r in c.execute("SELECT id FROM deletion_audit WHERE table_name='items' AND restored_at IS NULL ORDER BY id")]
                if not isinstance(ids,list) or not ids:return self.send(400,{'error':'请选择要还原的记录'})
                restored=[];skipped=[]
                for raw_id in ids[:200]:
                    try:aid=int(raw_id)
                    except:skipped.append({'id':raw_id,'reason':'编号无效'});continue
                    row=c.execute('SELECT * FROM deletion_audit WHERE id=?',(aid,)).fetchone()
                    if not row:skipped.append({'id':aid,'reason':'记录不存在'});continue
                    if row['restored_at']:skipped.append({'id':aid,'reason':'已还原'});continue
                    c.execute('SAVEPOINT restore_one')
                    try:
                        model=restore_snapshot(c,row,u['id'])
                        c.execute('RELEASE restore_one')
                        restored.append({'id':aid,'model':model})
                        log_action(c,u['id'],'还原物料',model,f'从回收站记录 #{aid} 还原')
                    except (ValueError,sqlite3.IntegrityError,TypeError) as e:
                        c.execute('ROLLBACK TO restore_one');c.execute('RELEASE restore_one')
                        skipped.append({'id':aid,'reason':str(e) or '还原失败'})
            return self.send(200,{'ok':True,'restored':len(restored),'items':restored,'skipped':skipped})
        if p=='/api/recycle/purge':
            u=self.require('can_edit')
            if not u:return
            if not u['is_admin']:return self.send(403,{'error':'仅管理员可彻底删除回收站记录'})
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取彻底删除请求'})
            with db() as c:
                ids=d.get('ids')
                if d.get('all'):
                    ids=[r['id'] for r in c.execute('SELECT id FROM deletion_audit ORDER BY id')]
                if not isinstance(ids,list) or not ids:return self.send(400,{'error':'请选择要彻底删除的记录'})
                purged=[];skipped=[]
                for raw_id in ids[:500]:
                    try:aid=int(raw_id)
                    except:skipped.append({'id':raw_id,'reason':'编号无效'});continue
                    row=c.execute('SELECT * FROM deletion_audit WHERE id=?',(aid,)).fetchone()
                    if not row:skipped.append({'id':aid,'reason':'记录不存在'});continue
                    try:
                        data=json.loads(row['payload'])
                        model=(data.get('item') or {}).get('model','') if isinstance(data,dict) else ''
                    except Exception:model=''
                    c.execute('DELETE FROM deletion_audit WHERE id=?',(aid,))
                    purged.append({'id':aid,'model':model})
                    log_action(c,u['id'],'彻底删除回收站记录',model or f'#{aid}',
                               f'永久移除删除前快照 #{aid}，不可再还原')
            return self.send(200,{'ok':True,'purged':len(purged),'items':purged,'skipped':skipped})
        if p=='/api/import-movements-preview':
            u=self.require('can_edit')
            if not u:return
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>IMPORT_LIMIT:return self.send(400,{'error':'请选择有效的 .xlsx 文件（最大 20 MB）'})
            raw=self.rfile.read(n)
            default_category={'optical':'光模块','cable':'线缆'}.get(self.headers.get('X-Category-Default',''),self.headers.get('X-Category-Default',''))
            try:result=parse_movement_import(raw,u['id'],default_category)
            except ValueError as e:return self.send(400,{'error':str(e)})
            except Exception:return self.send(400,{'error':'Excel 文件结构无法读取，请另存为标准 .xlsx 文件后重试'})
            return self.send(200,result)
        if p=='/api/import-movements-remap':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取字段映射请求'})
            token=str(d.get('edit_token',''))
            batch=MOVEMENT_IMPORT_TOKENS.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):
                return self.send(400,{'error':'预览已失效，请重新上传 Excel 文件'})
            try:
                header_row=int(d.get('header_row'))
                raw=d.get('mapping_index') or {}
                if not isinstance(raw,dict):raise ValueError('字段映射格式不正确')
                mapping={}; used=set()
                for field,col in raw.items():
                    if field not in MOVEMENT_FIELDS:continue
                    ix=int(col)
                    if ix<0 or ix>=len(batch['columns']):raise ValueError(f'列序号 {ix} 超出表格范围')
                    if ix in used:raise ValueError('同一列不能同时对应多个字段')
                    used.add(ix); mapping[field]=ix
                result=remap_movement_import(batch,header_row,mapping,token)
            except (ValueError,TypeError,KeyError) as e:
                return self.send(400,{'error':f'字段映射无效：{e}'})
            return self.send(200,result)
        if p=='/api/import-movements':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取导入确认请求'})
            token=str(d.get('token',''));batch=MOVEMENT_IMPORT_TOKENS.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):return self.send(400,{'error':'入库预览已失效，请重新上传文件'})
            edited=d.get('rows')
            if edited is not None:
                if not isinstance(edited,list) or len(edited)!=len(batch['rows']):return self.send(400,{'error':'编辑后的行数与预览不一致，请重新上传'})
                with db() as c:
                    items={norm_model(r['model']):dict(r) for r in c.execute('SELECT id,category,model,description FROM items')}
                rows=[];seen=[]
                try:
                    for i,raw in enumerate(edited,1):
                        model=str(raw.get('model','')).strip();key=norm_model(model);category=str(raw.get('category','')).strip();moved_at, _=movement_date(str(raw.get('moved_at','')).strip())
                        try:qty=parse_qty(raw.get('qty',0))
                        except ValueError as e:raise ValueError(f'第 {i} 行{e}')
                        if not model or key not in items:raise ValueError(f'第 {i} 行型号必须匹配现有物料台账')
                        if qty<=0:raise ValueError(f'第 {i} 行入库数量必须大于 0')
                        if category!=items[key]['category']:raise ValueError(f'第 {i} 行类别与型号台账不一致')
                        seen.append({'item_id':items[key]['id'],'category':category,'model':model,'description':items[key]['description'],'kind':'入库','qty':qty,'moved_at':moved_at,'location':str(raw.get('location','')).strip(),'owner':str(raw.get('owner','')).strip(),'notes':str(raw.get('notes','')).strip()})
                except (ValueError,TypeError) as e:return self.send(400,{'error':str(e) or '编辑内容格式错误'})
                batch['rows']=seen
            try:
                with db() as c:
                    for item in batch['rows']:
                        exists=c.execute('SELECT id FROM items WHERE id=?',(item['item_id'],)).fetchone()
                        if not exists:raise ValueError('物料在确认导入前已被删除，请重新预览文件')
                        c.execute('INSERT INTO movements(item_id,kind,qty,moved_at,location,owner,notes,created_by) VALUES(?,?,?,?,?,?,?,?)',(item['item_id'],'入库',item['qty'],item['moved_at'],item['location'],item['owner'],item['notes'],u['id']))
            except ValueError as e:return self.send(409,{'error':str(e)})
            except sqlite3.IntegrityError:return self.send(409,{'error':'导入未完成，请重新预览文件后重试'})
            MOVEMENT_IMPORT_TOKENS.pop(token,None)
            return self.send(200,{'ok':True,'imported':len(batch['rows'])})
        if p=='/api/import-items':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取导入确认请求'})
            token=str(d.get('token','')); batch=IMPORT_TOKENS.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):return self.send(400,{'error':'导入预览已失效，请重新上传文件'})
            if not batch.get('rows'):return self.send(400,{'error':'预览中没有可导入的数据，请先修正字段映射'})
            mode=batch.get('mode','create')
            edited=d.get('rows')
            if edited is not None:
                if not isinstance(edited,list) or len(edited)!=len(batch['rows']):return self.send(400,{'error':'编辑后的行数与预览不一致，请重新上传'})
                seen=set();clean=[]
                try:
                    for i,raw in enumerate(edited,1):
                        base=batch['rows'][i-1]
                        # 更新模式下型号是匹配键，不允许在预览里被改掉
                        model=(str(base.get('model','')) if mode=='update'
                               else str(raw.get('model','')).strip())
                        desc=str(raw.get('description','')).strip()
                        category=str(raw.get('category','')).strip()
                        planned_raw=raw.get('planned',-1)
                        if planned_raw is None or planned_raw=='' or isinstance(planned_raw,bool):
                            raise ValueError(f'第 {i} 行计划数量必须是大于等于 0 的整数')
                        planned=float(planned_raw)
                        if not planned.is_integer():raise ValueError(f'第 {i} 行计划数量必须是整数')
                        planned=int(planned)
                        if not model or not desc:raise ValueError(f'第 {i} 行型号和名称/规格不能为空')
                        if category not in ('光模块','线缆'):raise ValueError(f'第 {i} 行类别只能是光模块或线缆')
                        if planned<0:raise ValueError(f'第 {i} 行计划数量必须是大于等于 0 的整数')
                        key=norm_model(model)
                        if key in seen:raise ValueError(f'第 {i} 行型号在本批次重复（忽略大小写后）')
                        seen.add(key)
                        fields=validate_item_fields({'category':category,'model':model,'description':desc,
                            'planned':planned,'supplier':raw.get('supplier'),'location':raw.get('location'),
                            'owner':raw.get('owner'),'notes':raw.get('notes')})
                        fields['row']=base.get('row')
                        if mode=='update':fields['matched_id']=base.get('matched_id')
                        clean.append(fields)
                    with db() as c:
                        existing={r['model_key'] for r in c.execute('SELECT model_key FROM items')}
                    if mode=='create':
                        duplicate=seen & existing
                        if duplicate:raise ValueError('型号已存在于台账：'+next(iter(duplicate)))
                    else:
                        absent=seen-existing
                        if absent:raise ValueError('台账中找不到型号：'+next(iter(absent))+'，请重新预览')
                except (ValueError,TypeError) as e:
                    return self.send(400,{'error':str(e) or '编辑内容格式错误'})
                except Exception:
                    return self.send(400,{'error':'编辑内容格式错误，请检查预览表格中的数值和文本'})
                batch['rows']=clean
            try:
                with db() as c:
                    if mode=='update':
                        for item in batch['rows']:
                            rid=item.get('matched_id')
                            if not rid:raise ValueError('预览数据已失效，请重新上传文件')
                            cur=c.execute('''UPDATE items SET category=?,description=?,planned=?,supplier=?,location=?,owner=?,notes=? WHERE id=?''',
                                (item['category'],item['description'],item['planned'],item['supplier'],
                                 item['location'],item['owner'],item['notes'],rid))
                            if cur.rowcount==0:raise ValueError('物料在确认前已被删除，请重新预览')
                    else:
                        for item in batch['rows']:
                            c.execute('INSERT INTO items(category,model,model_key,description,supplier,planned,location,owner,notes) VALUES(?,?,?,?,?,?,?,?,?)',
                                (item['category'],item['model'],item['model_key'],item['description'],
                                 item['supplier'],item['planned'],item['location'],item['owner'],item['notes']))
            except ValueError as e:return self.send(409,{'error':str(e)})
            except sqlite3.IntegrityError:return self.send(409,{'error':'导入期间发现重复型号，整批未导入；请刷新预览后处理重复项'})
            with db() as c:
                log_action(c,u['id'],'批量更新物料' if mode=='update' else '导入物料',
                           f'{len(batch["rows"])} 条',f'模式：{mode}')
            IMPORT_TOKENS.pop(token,None)
            return self.send(200,{'ok':True,'imported':len(batch['rows']),'mode':mode})
        try: d=self.body()
        except Exception as e:return self.send(400,{'error':'无法读取提交内容'})
        if p=='/api/setup':
            with db() as c:
                if c.execute('SELECT count(*) FROM users').fetchone()[0]!=0:return self.send(409,{'error':'管理员已初始化'})
                username=str(d.get('username','')).strip(); password=str(d.get('password',''))
                if len(username)<3 or len(username)>40 or len(password)<12:return self.send(400,{'error':'用户名至少 3 位，管理员密码至少 12 位'})
                salt=secrets.token_bytes(16); cur=c.execute('INSERT INTO users(username,salt,pw_hash,is_admin,can_view,can_edit) VALUES(?,?,?,1,1,1)',(username,salt,pw_hash(password,salt))); uid=cur.lastrowid
            return self.make_session(uid)
        if p=='/api/login':
            username=str(d.get('username','')).strip(); password=str(d.get('password',''))
            with db() as c: u=c.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone()
            if not u or not hmac.compare_digest(bytes(u['pw_hash']),pw_hash(password,bytes(u['salt']))): return self.send(401,{'error':'用户名或密码不正确'})
            return self.make_session(u['id'])
        if p=='/api/logout':
            u=self.require(csrf=True)
            if not u:return
            token=''
            for part in self.headers.get('Cookie','').split(';'):
                if part.strip().startswith('sid='):token=part.strip()[4:]
            with db() as c:c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
            return self.send(200,{'ok':True},headers={'Set-Cookie':'sid=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
        if p=='/api/servers':
            u=self.require('can_edit')
            if not u:return
            room=str(d.get('room','')).strip();rack=str(d.get('rack','')).strip();sn=str(d.get('sn','')).strip()
            if not room or not rack or not sn:return self.send(400,{'error':'机房、机柜位置和 SN 均为必填项'})
            try:
                with db() as c:c.execute('INSERT INTO servers(room,rack,sn) VALUES(?,?,?)',(room,rack,sn))
            except sqlite3.IntegrityError:return self.send(409,{'error':'该 SN 已存在'})
            return self.send(201,{'ok':True})
        if p=='/api/items':
            u=self.require('can_edit')
            if not u:return
            try: fields=validate_item_fields(d)
            except ValueError as e:return self.send(400,{'error':str(e)})
            try:
                with db() as c:
                    if c.execute('SELECT 1 FROM items WHERE model_key=?',(fields['model_key'],)).fetchone():
                        return self.send(409,{'error':'该型号已存在；型号必须唯一（不区分大小写）'})
                    c.execute('INSERT INTO items(category,model,model_key,description,supplier,planned,location,owner,notes) VALUES(?,?,?,?,?,?,?,?,?)',
                        (fields['category'],fields['model'],fields['model_key'],fields['description'],
                         fields['supplier'],fields['planned'],fields['location'],fields['owner'],fields['notes']))
                    log_action(c,u['id'],'新增物料',fields['model'],f"计划数量 {fields['planned']}")
            except sqlite3.IntegrityError:return self.send(409,{'error':'该型号已存在；型号必须唯一（不区分大小写）'})
            return self.send(201,{'ok':True})
        if p=='/api/movements':
            u=self.require('can_edit')
            if not u:return
            kind=str(d.get('kind','')); moved=str(d.get('moved_at','')); model=str(d.get('model','')).strip(); category=str(d.get('category',''))
            try: qty=parse_qty(d.get('qty',0))
            except ValueError as e:return self.send(400,{'error':str(e)})
            if kind not in ('入库','领用/安装','报废') or not moved:return self.send(400,{'error':'业务类型、日期必填'})
            if category not in ('光模块','线缆'):return self.send(400,{'error':'类别必填且只能是光模块或线缆'})
            try:
                datetime.strptime(str(moved),'%Y-%m-%d')
            except (ValueError,TypeError):
                return self.send(400,{'error':'日期格式必须是 YYYY-MM-DD，例如 2026-11-12'})
            try:
                with db() as c:
                    matches=c.execute('SELECT id,model,category FROM items WHERE model_key=?',(norm_model(model),)).fetchall()
                    if not matches:return self.send(404,{'error':'型号不存在'})
                    if len(matches)>1:return self.send(409,{'error':'该型号不唯一，无法关联'})
                    it=matches[0]; item_id=it['id']
                    if category and category!=it['category']:return self.send(400,{'error':'类别与该型号台账不一致'})
                    cur=c.execute('INSERT INTO movements(item_id,kind,qty,moved_at,location,owner,notes,created_by) VALUES(?,?,?,?,?,?,?,?)',(item_id,kind,qty,moved,str(d.get('location','')),str(d.get('owner',d.get('handler',''))),str(d.get('notes','')),u['id']))
                    if kind=='报废':
                        c.executemany('INSERT INTO material_spares(movement_id,unit_index,item_id,created_by) VALUES(?,?,?,?)',[(cur.lastrowid,i,item_id,u['id']) for i in range(1,qty+1)])
                    if kind!='入库':
                        assert_stock(c,[item_id],f'登记{kind} {qty} 件')
                    log_action(c,u['id'],'登记'+kind,model,f'数量 {qty}，日期 {moved}')
            except ValueError as e:return self.send(409,{'error':str(e)})
            except sqlite3.IntegrityError:return self.send(400,{'error':'明细数据无效'})
            return self.send(201,{'ok':True})
        if p=='/api/users':
            u=self.require(csrf=True)
            if not u:return
            if not u['is_admin']:return self.send(403,{'error':'仅管理员可创建账号'})
            username=str(d.get('username','')).strip(); password=str(d.get('password',''))
            if len(username)<3 or len(password)<12:return self.send(400,{'error':'用户名至少 3 位，新账号密码至少 12 位'})
            salt=secrets.token_bytes(16)
            try:
                with db() as c:c.execute('INSERT INTO users(username,salt,pw_hash,is_admin,can_view,can_edit) VALUES(?,?,?,0,?,?)',(username,salt,pw_hash(password,salt),int(bool(d.get('can_view')) or bool(d.get('can_edit'))),int(bool(d.get('can_edit')))))
            except sqlite3.IntegrityError:return self.send(409,{'error':'用户名已存在'})
            return self.send(201,{'ok':True})
        return self.send(404,{'error':'没有这个接口'})
    def do_PATCH(self):
        p=urllib.parse.urlparse(self.path).path
        if p.startswith('/api/material-spares/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body();spare_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'坏件记录格式无效'})
            serial=str(d.get('serial_number','')).strip();status=str(d.get('status','')).strip();responsible=str(d.get('responsible','')).strip() or u['username'];notes=str(d.get('notes','')).strip()
            if not serial or len(serial)>150:return self.send(400,{'error':'SN必填，最多 150 字'})
            if status not in MATERIAL_SPARE_STATUSES or status=='待处理':return self.send(400,{'error':'请填写SN并选择处理状态'})
            if len(responsible)>100:return self.send(400,{'error':'负责人最多 100 字'})
            if len(notes)>SPARE_NOTES_LIMIT:return self.send(400,{'error':f'备注最多 {SPARE_NOTES_LIMIT} 字'})
            try:
                with db() as c:
                    old=c.execute('SELECT * FROM material_spares WHERE id=?',(spare_id,)).fetchone()
                    if not old:return self.send(404,{'error':'坏件记录不存在'})
                    duplicate=c.execute('SELECT id FROM material_spares WHERE item_id=? AND serial_number=? AND id<>?',(old['item_id'],serial,spare_id)).fetchone()
                    if duplicate:return self.send(409,{'error':'同一型号下该SN已登记'})
                    c.execute('UPDATE material_spares SET serial_number=?,status=?,responsible=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(serial,status,responsible,notes,spare_id))
                    if old['serial_number']!=serial or old['status']!=status or old['responsible']!=responsible or old['notes']!=notes:
                        c.execute('''INSERT INTO material_spare_history(spare_id,from_status,to_status,old_serial_number,serial_number,responsible,notes,operated_by)
                            VALUES(?,?,?,?,?,?,?,?)''',(spare_id,old['status'],status,old['serial_number'],serial,responsible,notes,u['id']))
            except sqlite3.IntegrityError:return self.send(409,{'error':'坏件记录更新冲突，请刷新后重试'})
            return self.send(200,{'ok':True})
        if p.startswith('/api/material-spare-cases/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body();case_id=int(p.rsplit('/',1)[1]);returned_qty=int(d.get('returned_qty',0));scrapped_qty=int(d.get('scrapped_qty',0))
            except:return self.send(400,{'error':'RMA处理数量格式无效'})
            status=str(d.get('status','')).strip();responsible=str(d.get('responsible','')).strip();notes=str(d.get('notes','')).strip()
            if status not in MATERIAL_SPARE_STATUSES:return self.send(400,{'error':'备件状态无效'})
            if not responsible or len(responsible)>100:return self.send(400,{'error':'实际操作人必填，最多 100 字'})
            try:
                with db() as c:
                    old=c.execute('SELECT * FROM material_spare_cases WHERE id=?',(case_id,)).fetchone()
                    if not old:return self.send(404,{'error':'RMA备件记录不存在'})
                    active=('待RMA','RMA处理中')
                    if old['status'] not in active and status!=old['status']:return self.send(409,{'error':'该RMA记录已结束，不能再次改变处置结果'})
                    if old['status']=='待RMA' and status=='RMA已完成':return self.send(400,{'error':'请先将状态更新为RMA处理中，再登记RMA结果'})
                    if returned_qty<old['returned_qty'] or scrapped_qty<old['scrapped_qty'] or returned_qty+scrapped_qty>old['qty']:
                        return self.send(400,{'error':'返还和报废数量必须递增，且合计不能超过登记数量'})
                    if status=='报废':scrapped_qty=old['qty']-returned_qty
                    if status=='RMA已完成' and returned_qty+scrapped_qty!=old['qty']:
                        return self.send(400,{'error':'RMA已完成时，必须登记全部数量的返还或报废结果'})
                    if status in active and returned_qty+scrapped_qty>=old['qty']:
                        return self.send(400,{'error':'该数量已全部返还或报废，请完成RMA记录'})
                    scrap_delta=scrapped_qty-old['scrapped_qty']
                    if scrap_delta>0:
                        c.execute("INSERT INTO movements(item_id,kind,qty,moved_at,location,owner,notes,created_by) VALUES(?,'报废',?,?,?,?,?,?)",(old['item_id'],scrap_delta,date.today().isoformat(),'RMA处置',responsible,notes or f'RMA备件记录 #{case_id} 报废',u['id']))
                    c.execute('UPDATE material_spare_cases SET returned_qty=?,scrapped_qty=?,status=?,responsible=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(returned_qty,scrapped_qty,status,responsible,notes,case_id))
                    if status!=old['status'] or returned_qty!=old['returned_qty'] or scrapped_qty!=old['scrapped_qty'] or responsible!=old['responsible'] or notes!=old['notes']:
                        c.execute('''INSERT INTO material_spare_case_history(case_id,from_status,to_status,qty_returned,qty_scrapped,responsible,notes,operated_by)
                            VALUES(?,?,?,?,?,?,?,?)''',(case_id,old['status'],status,returned_qty,scrapped_qty,responsible,notes,u['id']))
            except sqlite3.IntegrityError:return self.send(409,{'error':'RMA状态更新冲突，请刷新后重试'})
            return self.send(200,{'ok':True})
        if p.startswith('/api/server-spares/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body(); spare_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'备件记录格式无效'})
            status=str(d.get('status','')).strip();responsible=str(d.get('responsible','')).strip();notes=str(d.get('notes','')).strip()
            if status not in SERVER_SPARE_STATUSES:return self.send(400,{'error':'备件状态无效'})
            if not responsible or len(responsible)>100:return self.send(400,{'error':'实际操作人必填，最多 100 字'})
            if len(notes)>SPARE_NOTES_LIMIT:return self.send(400,{'error':f'备注最多 {SPARE_NOTES_LIMIT} 字'})
            try:
                with db() as c:
                    old=c.execute('SELECT status FROM server_spares WHERE id=?',(spare_id,)).fetchone()
                    if not old:return self.send(404,{'error':'服务器备件记录不存在'})
                    c.execute('UPDATE server_spares SET status=?,responsible=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(status,responsible,notes,spare_id))
                    if old['status']!=status:
                        c.execute('INSERT INTO server_spare_status_history(spare_id,from_status,to_status,responsible,notes,operated_by) VALUES(?,?,?,?,?,?)',(spare_id,old['status'],status,responsible,notes,u['id']))
            except sqlite3.IntegrityError:return self.send(409,{'error':'状态更新失败，请刷新后重试'})
            return self.send(200,{'ok':True})
        if p.startswith('/api/server-components/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body(); component_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'部件数据格式无效'})
            node_sn=str(d.get('node_sn','')).strip(); responsible=str(d.get('responsible','')).strip(); action=str(d.get('action','信息修正'))
            if not node_sn or not responsible or len(responsible)>100:return self.send(400,{'error':'新 NODE_SN 和责任人必填（责任人最多 100 字）'})
            if action not in ('信息修正','部件更换'):return self.send(400,{'error':'操作类型无效'})
            if action=='部件更换' and node_sn=='' :return self.send(400,{'error':'更换后的 NODE_SN 不能为空'})
            try:
                with db() as c:
                    old=c.execute('SELECT server_sn,node_sn,node_u,kp_name,bmc_mac FROM server_components WHERE id=?',(component_id,)).fetchone()
                    if not old:return self.send(404,{'error':'该 U 位部件不存在'})
                    srv=c.execute('SELECT id FROM servers WHERE sn=?',(old['server_sn'],)).fetchone()
                    server_id=srv['id'] if srv else ARCHIVED_SERVER_ID
                    if action=='部件更换' and node_sn==old['node_sn']:return self.send(400,{'error':'更换后的 NODE_SN 与当前相同，请确认更换编号'})
                    old_data=dict(old)
                    new_data={'node_sn':node_sn,'node_u':old['node_u'],'kp_name':old['kp_name'],'bmc_mac':old['bmc_mac']}
                    c.execute('UPDATE server_components SET node_sn=? WHERE id=?',(node_sn,component_id))
                    op=c.execute('INSERT INTO server_component_operations(server_sn,server_id,node_u,action,responsible,old_data,new_data,operated_by) VALUES(?,?,?,?,?,?,?,?)',(old['server_sn'],server_id,old['node_u'],action,responsible,json.dumps(old_data,ensure_ascii=False),json.dumps(new_data,ensure_ascii=False),u['id']))
                    if action=='部件更换' and old['node_sn']!=node_sn:
                        place=c.execute('SELECT room,rack FROM servers WHERE sn=?',(old['server_sn'],)).fetchone()
                        room,rack=(place['room'],place['rack']) if place else ('','')
                        spare=c.execute('''INSERT INTO server_spares(operation_id,server_sn,server_id,node_sn,node_u,kp_name,bmc_mac,room,rack,status,responsible,created_by)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(op.lastrowid,old['server_sn'],server_id,old['node_sn'],old['node_u'],old['kp_name'],old['bmc_mac'],room,rack,'待检测',responsible,u['id']))
                        c.execute('''INSERT INTO server_spare_status_history(spare_id,from_status,to_status,responsible,notes,operated_by)
                            VALUES(?,?,?,?,?,?)''',(spare.lastrowid,'更换下架','待检测',responsible,'部件更换时自动转入服务器备件台账',u['id']))
            except sqlite3.IntegrityError:return self.send(409,{'error':'目标 U 位已占用，或 NODE_SN 已在本服务器使用'})
            return self.send(200,{'ok':True})
        if p.startswith('/api/servers/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body();server_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'服务器记录格式无效'})
            room=str(d.get('room','')).strip();rack=str(d.get('rack','')).strip();sn=str(d.get('sn','')).strip()
            if not room or not rack or not sn:return self.send(400,{'error':'机房、机柜位置和 SN 均为必填项'})
            try:
                with db() as c:
                    cur=c.execute('UPDATE servers SET room=?,rack=?,sn=? WHERE id=?',(room,rack,sn,server_id))
                    if cur.rowcount==0:return self.send(404,{'error':'服务器记录不存在'})
            except sqlite3.IntegrityError:return self.send(409,{'error':'该 SN 已被其他服务器使用'})
            return self.send(200,{'ok':True})
        if p.startswith('/api/movements/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body(); movement_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'明细数据格式无效'})
            try:qty=parse_qty(d.get('qty',0))
            except ValueError as e:return self.send(400,{'error':str(e)})
            model=str(d.get('model','')).strip(); kind=str(d.get('kind','')); moved=str(d.get('moved_at',''))
            if not model or not moved or kind not in ('入库','领用/安装','报废'):return self.send(400,{'error':'型号、日期和有效业务类型必填'})
            with db() as c:
                items=c.execute('SELECT id FROM items WHERE model=?',(model,)).fetchall()
                if len(items)!=1:return self.send(400,{'error':'型号不存在或不唯一'})
                item=c.execute('SELECT category FROM items WHERE id=?',(items[0]['id'],)).fetchone()
                if d.get('category') and d.get('category')!=item['category']:return self.send(400,{'error':'类别需与型号对应的台账类别一致'})
                old=c.execute('SELECT * FROM movements WHERE id=?',(movement_id,)).fetchone()
                if not old:return self.send(404,{'error':'库存变动明细不存在'})
                linked=c.execute('SELECT COUNT(*) n FROM material_spares WHERE movement_id=?',(movement_id,)).fetchone()['n']
                progressed=c.execute("SELECT COUNT(*) n FROM material_spares WHERE movement_id=? AND (serial_number<>'' OR status<>'待处理')",(movement_id,)).fetchone()['n']
                if linked and progressed and (kind!='报废' or old['item_id']!=items[0]['id'] or qty<old['qty']):
                    return self.send(409,{'error':'该报废明细已有已处理坏件，不能更改型号、业务类型或减少数量'})
                if predict_negative(c,movement_id,items[0]['id'],kind,qty):
                    return self.send(409,{'error':'可用库存不足：修改后会出现负库存，不允许负库存，本次修改已撤销'})
                cur=c.execute('UPDATE movements SET item_id=?,kind=?,qty=?,moved_at=?,location=?,owner=?,notes=? WHERE id=?',(items[0]['id'],kind,qty,moved,str(d.get('location','')),str(d.get('owner','')),str(d.get('notes','')),movement_id))
                if linked:
                    if kind!='报废':c.execute('DELETE FROM material_spares WHERE movement_id=?',(movement_id,))
                    elif not progressed and old['item_id']!=items[0]['id']:c.execute('DELETE FROM material_spares WHERE movement_id=?',(movement_id,))
                    if kind=='报废':
                        c.execute('DELETE FROM material_spares WHERE movement_id=? AND unit_index>?',(movement_id,qty))
                        existing=c.execute('SELECT COALESCE(MAX(unit_index),0) n FROM material_spares WHERE movement_id=?',(movement_id,)).fetchone()['n']
                        c.executemany('INSERT INTO material_spares(movement_id,unit_index,item_id,created_by) VALUES(?,?,?,?)',[(movement_id,i,items[0]['id'],u['id']) for i in range(existing+1,qty+1)])
                elif kind=='报废':
                    c.executemany('INSERT INTO material_spares(movement_id,unit_index,item_id,created_by) VALUES(?,?,?,?)',[(movement_id,i,items[0]['id'],u['id']) for i in range(1,qty+1)])
            return self.send(200,{'ok':True})
        if p.startswith('/api/items/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body(); item_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'请求格式无效'})
            unknown=[k for k in d if k not in ITEM_FIELDS]
            if unknown:return self.send(400,{'error':'不支持修改的字段：'+ '、'.join(sorted(unknown))})
            try:fields=validate_item_fields(d,partial=True)
            except ValueError as e:return self.send(400,{'error':str(e)})
            if not fields:return self.send(400,{'error':'没有需要修改的字段'})
            with db() as c:
                cur=c.execute('SELECT id,model_key FROM items WHERE id=?',(item_id,)).fetchone()
                if not cur:return self.send(404,{'error':'物料不存在'})
                if 'model_key' in fields and fields['model_key']!=cur['model_key']:
                    if c.execute('SELECT 1 FROM items WHERE model_key=? AND id<>?',(fields['model_key'],item_id)).fetchone():
                        return self.send(409,{'error':'该型号已存在；型号必须唯一（不区分大小写）'})
                sets=', '.join(f'{k}=?' for k in fields)
                c.execute(f'UPDATE items SET {sets} WHERE id=?',list(fields.values())+[item_id])
                log_action(c,u['id'],'编辑物料',fields.get('model') or f'#{item_id}',
                           '修改字段：'+ '、'.join(sorted(set(fields)&set(ITEM_FIELDS))))
            return self.send(200,{'ok':True,'updated':sorted(set(fields)&set(ITEM_FIELDS))})
        if not p.startswith('/api/users/'):return self.send(404,{'error':'没有这个接口'})
        u=self.require(csrf=True)
        if not u:return
        if not u['is_admin']:return self.send(403,{'error':'仅管理员可管理账号'})
        try:d=self.body(); uid=int(p.rsplit('/',1)[1])
        except:return self.send(400,{'error':'请求格式无效'})
        if uid==u['id']:return self.send(400,{'error':'不能停用当前管理员账号'})
        with db() as c:
            target=c.execute('SELECT id,is_admin FROM users WHERE id=?',(uid,)).fetchone()
            if not target or target['is_admin']:return self.send(404,{'error':'协管员不存在'})
            active=int(bool(d.get('active',True))); edit=int(bool(d.get('can_edit'))); view=int(bool(d.get('can_view')) or bool(edit))
            c.execute('UPDATE users SET active=?,can_view=?,can_edit=? WHERE id=?',(active,view,edit,uid))
            if not active:c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        return self.send(200,{'ok':True})
    def make_session(self,uid):
        token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24); th=hashlib.sha256(token.encode()).hexdigest()
        with db() as c:
            c.execute('INSERT INTO sessions(token_hash,user_id,csrf,expires) VALUES(?,?,?,?)',(th,uid,csrf,int(time.time())+SESSION_SECONDS))
            u=c.execute('SELECT id,username,is_admin,can_view,can_edit FROM users WHERE id=?',(uid,)).fetchone()
        return self.send(200,dict(u)|{'csrf':csrf},headers={'Set-Cookie':f'sid={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}'})
    def do_DELETE(self):
        p=urllib.parse.urlparse(self.path).path
        u=self.require(csrf=True)
        if not u:return
        if not u['is_admin']:return self.send(403,{'error':'仅管理员可删除记录'})
        try: record_id=int(p.rsplit('/',1)[1])
        except:return self.send(400,{'error':'记录编号无效'})
        try: d=self.body() if int(self.headers.get('Content-Length','0') or 0)>0 else {}
        except Exception: d={}
        with db() as c:
            if p.startswith('/api/server-spares/'):
                spare=c.execute('''SELECT s.id,s.server_id,sv.sn AS current_sn
                    FROM server_spares s LEFT JOIN servers sv ON sv.id=s.server_id
                    WHERE s.id=?''',(record_id,)).fetchone()
                if not spare:return self.send(404,{'error':'服务器备件记录不存在'})
                if spare['server_id'] and spare['server_id']>0 and spare['current_sn']:
                    return self.send(409,{'error':'该备件仍关联现有服务器，不能单独删除；如需清理请先删除对应服务器，历史归档后再处理。'})
                c.execute('DELETE FROM server_spare_status_history WHERE spare_id=?',(record_id,))
                c.execute('DELETE FROM server_spares WHERE id=?',(record_id,))
                log_action(c,u['id'],'彻底删除服务器备件',f'#{record_id}','清理已归档孤儿备件及状态历史；更换操作记录保留')
                return self.send(200,{'ok':True})
            if p.startswith('/api/servers/'):
                sv=c.execute('SELECT sn FROM servers WHERE id=?',(record_id,)).fetchone()
                if not sv:return self.send(404,{'error':'服务器记录不存在'})
                counts={'components':c.execute('SELECT COUNT(*) n FROM server_components WHERE server_sn=?',(sv['sn'],)).fetchone()['n'],
                        'operations':c.execute('SELECT COUNT(*) n FROM server_component_operations WHERE server_id=?',(record_id,)).fetchone()['n'],
                        'spares':c.execute('SELECT COUNT(*) n FROM server_spares WHERE server_id=?',(record_id,)).fetchone()['n'],
                        'history':c.execute('''SELECT COUNT(*) n FROM server_spare_status_history h
                            JOIN server_spares s ON s.id=h.spare_id WHERE s.server_id=?''',(record_id,)).fetchone()['n']}
                if sum(counts.values()) and str(d.get('confirm_sn','')).strip()!=str(sv['sn']).strip():
                    return self.send(409,{
                        'error':f'该服务器有 {counts["components"]} 条 46U 部件、{counts["operations"]} 条更换/修正历史、'
                                f'{counts["spares"]} 条备件及 {counts["history"]} 条状态历史。删除会移除部件清单，'
                                f'更换历史与备件将归档为「原服务器已删除」（不再关联任何服务器，也不会错挂到复用的 SN）。'
                                f'如确需删除，请输入完整 SN「{sv["sn"]}」再次确认。',
                        'counts':counts,'sn':sv['sn'],'requires_confirm':True})
                # 归档：断开 id 关联，历史与备件本身保留供追溯，绝不按 SN 重新挂回新机器
                c.execute('UPDATE server_component_operations SET server_id=? WHERE server_id=?',(ARCHIVED_SERVER_ID,record_id))
                c.execute('UPDATE server_spares SET server_id=? WHERE server_id=?',(ARCHIVED_SERVER_ID,record_id))
                c.execute('DELETE FROM servers WHERE id=?',(record_id,))
                log_action(c,u['id'],'删除服务器',f'#{record_id}','部件清单已删除；更换历史与备件已归档')
                return self.send(200,{'ok':True,'archived':counts})
            if p.startswith('/api/items/'):
                it=c.execute('SELECT * FROM items WHERE id=?',(record_id,)).fetchone()
                if not it:return self.send(404,{'error':'物料不存在'})
                counts={'movements':c.execute('SELECT COUNT(*) n FROM movements WHERE item_id=?',(record_id,)).fetchone()['n'],
                        'spares':c.execute('SELECT COUNT(*) n FROM material_spares WHERE item_id=?',(record_id,)).fetchone()['n'],
                        'cases':c.execute('SELECT COUNT(*) n FROM material_spare_cases WHERE item_id=?',(record_id,)).fetchone()['n']}
                if sum(counts.values()) and str(d.get('confirm_model','')).strip()!=str(it['model']).strip():
                    return self.send(409,{
                        'error':f'该物料已有 {counts["movements"]} 条库存变动明细、{counts["spares"]} 条坏件 SN 记录、{counts["cases"]} 条备件单。删除会一并抹掉这些历史且无法恢复。如确需删除，请输入完整型号「{it["model"]}」再次确认。',
                        'counts':counts,'model':it['model'],'requires_confirm':True})
                # 删除前留完整快照，便于事后追溯/恢复
                def fetch(sql,args=()):return [dict(r) for r in c.execute(sql,args).fetchall()]
                spare_ids=[r['id'] for r in c.execute('SELECT id FROM material_spares WHERE item_id=?',(record_id,)).fetchall()]
                case_ids=[r['id'] for r in c.execute('SELECT id FROM material_spare_cases WHERE item_id=?',(record_id,)).fetchall()]
                snapshot={'item':dict(it),
                          'movements':fetch('SELECT * FROM movements WHERE item_id=?',(record_id,)),
                          'material_spares':fetch('SELECT * FROM material_spares WHERE item_id=?',(record_id,)),
                          'material_spare_cases':fetch('SELECT * FROM material_spare_cases WHERE item_id=?',(record_id,))}
                for key,table,col,ids in (('material_spare_history','material_spare_history','spare_id',spare_ids),
                                          ('material_spare_case_history','material_spare_case_history','case_id',case_ids)):
                    if ids:
                        ph=','.join('?'*len(ids))
                        snapshot[key]=fetch(f'SELECT * FROM {table} WHERE {col} IN ({ph})',ids)
                c.execute('INSERT INTO deletion_audit(table_name,record_id,payload,deleted_by) VALUES(?,?,?,?)',
                          ('items',record_id,json.dumps(snapshot,ensure_ascii=False,default=str),u['id']))
                removed=c.execute('DELETE FROM movements WHERE item_id=?',(record_id,)).rowcount
                c.execute('DELETE FROM items WHERE id=?',(record_id,))
                log_action(c,u['id'],'删除物料',it['model'],
                           f"删除 {removed} 条明细、{counts['spares']} 条坏件记录；快照已进入回收站")
                return self.send(200,{'ok':True,'deleted_movements':removed,'audit_saved':True})
            if p.startswith('/api/movements/'):
                linked=c.execute('SELECT COUNT(*) n FROM material_spares WHERE movement_id=? AND (serial_number<>\'\' OR status<>\'待处理\')',(record_id,)).fetchone()['n']
                if linked:return self.send(409,{'error':'该报废明细已有已处理坏件，不能删除；请保留明细及SN追溯记录'})
                old=c.execute('SELECT item_id,kind,qty FROM movements WHERE id=?',(record_id,)).fetchone()
                if not old:return self.send(404,{'error':'明细不存在'})
                after=stock_available(c,old['item_id'])+(-old['qty'] if old['kind']=='入库' else old['qty'])
                if after<0:
                    return self.send(409,{'error':f'删除后可用库存将变为 {after}，不允许负库存，本次删除已撤销'})
                cur=c.execute('DELETE FROM movements WHERE id=?',(record_id,))
                log_action(c,u['id'],'删除库存变动明细',f'#{record_id}','该操作不进入回收站')
                return self.send(200,{'ok':True})
        return self.send(404,{'error':'没有这个接口'})
    def do_OPTIONS(self): self.send(204,b'')

if __name__=='__main__':
    init(); print(f'IDC资产管理已启动: http://{HOST}:{PORT}'); ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
