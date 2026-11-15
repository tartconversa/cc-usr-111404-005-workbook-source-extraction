"""沿关系定位目标 table，并按列身份映射字段。

定位顺序固定为：包根关系→workbook→[Content_Types] 确认主部件类型→
工作表关系→工作表 tableParts→table 部件。表名以 table 的 name 匹配，
而不是按工作表顺序或固定列偏移；重复表名在领取阶段拒绝，因此不会悄悄
选第一张。关系目标外链、越界或缺失见 ooxml.Package.resolve。
"""
import posixpath
from urllib.parse import unquote

from .data import parse_area
from .errors import CatalogError
from .ooxml import attr, local_name, relationship_id

_MAIN_CONTENT_TYPES={
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml',
    'application/vnd.ms-excel.sheet.macroEnabled.main+xml',
    'application/vnd.ms-excel.template.macroEnabled.main+xml',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml',
}
_WORKSHEET_CONTENT_TYPES={
    'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml',
    'application/vnd.ms-excel.worksheet+xml',
}
_TABLE_CONTENT_TYPES={
    'application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml',
    'application/vnd.ms-excel.table+xml',
}
_SHARED_STRINGS_CONTENT_TYPES={
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml',
    'application/vnd.ms-excel.sharedStrings+xml',
}
_STYLES_CONTENT_TYPES={
    'application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml',
    'application/vnd.ms-excel.styles+xml',
}
_XLSX_MEDIA_TYPES={
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-excel.sheet.macroEnabled.12',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.template',
    'application/vnd.ms-excel.template.macroEnabled.12',
}
_TABLE_CHILDREN={'autoFilter','sortState','tableColumns','tableStyleInfo','extLst'}


def _is_content_type(value,allowed):
    return value is not None and value.lower() in {item.lower() for item in allowed}


def check_media_type(headers):
    content_type=None
    for key,value in headers:
        if key.lower()=='content-type':content_type=value
    if content_type is None:return None
    declared=content_type.split(';',1)[0].strip()
    media=declared.lower()
    if media=='application/octet-stream':return None
    if media not in {value.lower() for value in _XLSX_MEDIA_TYPES}:
        raise CatalogError('media_type_unsupported','来源实体不是既定的 Excel 工作簿媒体类型',{'package':True},content_type=media)
    return declared


class WorkbookLayout:
    """一份工作簿的部件图。工作表顺序来自 workbook.xml，不从 ZIP 顺序推断。"""

    def __init__(self,package):
        self.package=package
        self._load_content_types()
        self._resolve_workbook()
        self._load_workbook()
        self._load_shared_strings()
        self._load_styles()
        self._scan_auxiliary_relationships()
        self._table_cache={}

    def _load_content_types(self):
        part='/[Content_Types].xml'
        if not self.package.exists(part):
            raise CatalogError('package_content_types_missing','包缺少内容类型部件',{'part':part})
        root=self.package.xml_root(part)
        if local_name(root.tag)!='Types':
            raise CatalogError('package_content_types_malformed','内容类型部件根元素不是 Types',{'part':part})
        self._defaults={};self._overrides={}
        for elem in root:
            name=local_name(elem.tag)
            if name=='Default':
                extension=(attr(elem,'Extension') or '').lower();content=attr(elem,'ContentType')
                if not extension or not content:
                    raise CatalogError('package_content_types_malformed','Default 缺少 Extension 或 ContentType',{'part':part})
                if extension in self._defaults:
                    raise CatalogError('package_content_types_duplicate','内容类型扩展名重复',{'part':part},extension=extension)
                self._defaults[extension]=content
            elif name=='Override':
                override=unquote(attr(elem,'PartName') or '');content=attr(elem,'ContentType')
                if not override or not content:
                    raise CatalogError('package_content_types_malformed','Override 缺少 PartName 或 ContentType',{'part':part})
                if override in self._overrides:
                    raise CatalogError('package_content_types_duplicate','内容类型部件名重复',{'part':part},override=override)
                self._overrides[override]=content
            else:
                raise CatalogError('package_content_types_malformed','内容类型部件含未知元素',{'part':part},element=name)

    def content_type(self,part):
        if part in self._overrides:return self._overrides[part]
        extension=posixpath.splitext(part)[1].lstrip('.').lower()
        return self._defaults.get(extension)

    def _resolve_workbook(self):
        rels_part,rels=self.package.related('/',kind='officeDocument')
        if not rels:
            raise CatalogError('workbook_main_missing','包根关系没有登记 officeDocument 主部件',
                               {'part':rels_part,'rels_part':rels_part})
        if len(rels)>1:
            raise CatalogError('workbook_main_ambiguous','包根登记了多个 officeDocument 关系',
                               {'part':rels_part,'rels_part':rels_part},relationships=[rel.id for rel in rels])
        self.workbook_part=self.package.resolve(rels[0])
        content=self.content_type(self.workbook_part)
        if not _is_content_type(content,_MAIN_CONTENT_TYPES):
            raise CatalogError('workbook_content_type','工作簿主部件内容类型不是既定主类型',{'part':self.workbook_part},content_type=content)

    def _load_workbook(self):
        root=self.package.xml_root(self.workbook_part)
        if local_name(root.tag)!='workbook':
            raise CatalogError('workbook_malformed','主部件根元素不是 workbook',{'part':self.workbook_part})
        self.date_1904=False;self.calc_pr=[]
        sheets_elem=None
        for elem in root:
            name=local_name(elem.tag)
            if name=='workbookPr':
                value=attr(elem,'date1904')
                if value is not None:
                    folded=value.strip().lower()
                    if folded not in ('0','1','true','false','on','off'):
                        raise CatalogError('invalid_boolean','date1904 不是合法布尔值',{'part':self.workbook_part},value=value)
                    self.date_1904=folded in ('1','true','on')
            elif name=='calcPr':
                self.calc_pr.append({'calcId':attr(elem,'calcId'),'fullCalcOnLoad':attr(elem,'fullCalcOnLoad'),
                                     'note':'calcPr 只是来源提示，不代表缓存已重新计算或新鲜'})
            elif name=='sheets':
                if sheets_elem is not None:
                    raise CatalogError('sheets_duplicate','工作簿含多个 sheets 元素',{'part':self.workbook_part})
                sheets_elem=elem
        if sheets_elem is None:raise CatalogError('sheets_missing','工作簿没有 sheets 元素',{'part':self.workbook_part})
        self.sheets=[];seen_names={};seen_sheet_ids={}
        _,rels=self.package.related(self.workbook_part)
        by_id={rel.id:rel for rel in rels}
        for sheet in sheets_elem:
            if local_name(sheet.tag)!='sheet':
                raise CatalogError('sheets_malformed','sheets 含未知子元素',{'part':self.workbook_part},element=local_name(sheet.tag))
            order=len(self.sheets)+1
            if order>self.package.limits.max_sheets:
                raise CatalogError('sheets_budget','工作表数量超过预算',{'part':self.workbook_part},limit=self.package.limits.max_sheets)
            name=attr(sheet,'name')
            if not name:raise CatalogError('sheet_name_missing','工作表缺少 name',{'part':self.workbook_part,'sheet_order':order})
            if name in seen_names:
                raise CatalogError('sheet_name_duplicate','工作表名重复，无法按名称定位',{'part':self.workbook_part,'sheet_name':name,'sheet_order':order})
            seen_names[name]=order
            sheet_id=attr(sheet,'sheetId')
            if sheet_id is not None:
                if not str(sheet_id).isdigit():
                    raise CatalogError('sheet_id_invalid','工作表 sheetId 不是整数',{'part':self.workbook_part,'sheet_name':name},sheet_id=sheet_id)
                if sheet_id in seen_sheet_ids:
                    raise CatalogError('sheet_id_duplicate','工作表 sheetId 重复',{'part':self.workbook_part,'sheet_name':name,'sheet_order':order},sheet_id=sheet_id,first=seen_sheet_ids[sheet_id])
                seen_sheet_ids[sheet_id]=name
            state=attr(sheet,'state') or 'visible'
            if state not in ('visible','hidden','veryHidden'):
                raise CatalogError('sheet_state_invalid','工作表可见状态非法',{'part':self.workbook_part,'sheet_name':name},state=state)
            rel_id=relationship_id(sheet)
            if not rel_id:
                raise CatalogError('sheet_relationship_missing','工作表缺少关系 Id',{'part':self.workbook_part,'sheet_name':name})
            rel=by_id.get(rel_id)
            if rel is None:
                raise CatalogError('sheet_relationship_missing','工作表的关系 Id 未在 workbook.rels 中登记',
                                   {'part':self.workbook_part,'sheet_name':name,'relationship_id':rel_id})
            if rel.kind!='worksheet':
                raise CatalogError('sheet_relationship_kind','工作表关系类型不是 worksheet',rel.location(),sheet_name=name,kind=rel.kind)
            part=self.package.resolve(rel)
            content=self.content_type(part)
            if not _is_content_type(content,_WORKSHEET_CONTENT_TYPES):
                raise CatalogError('worksheet_content_type','工作表内容类型不是既定工作表类型',{'part':part,'sheet_name':name},content_type=content)
            self.sheets.append({'name':name,'sheet_id':attr(sheet,'sheetId'),'state':state,'order':order,
                                'part':part,'relationship_id':rel_id,'content_type':content})

    def _load_shared_strings(self):
        self.shared_strings=[]
        self.shared_strings_part=None
        self.shared_strings_declared=None
        rel=self.package.find_related(self.workbook_part,'sharedStrings')
        if rel is None:return
        part=self.package.resolve(rel)
        if not _is_content_type(self.content_type(part),_SHARED_STRINGS_CONTENT_TYPES):
            raise CatalogError('shared_strings_content_type','共享字符串部件内容类型不符',{'part':part},content_type=self.content_type(part))
        self.shared_strings_part=part
        root=self.package.xml_root(part)
        if local_name(root.tag)!='sst':raise CatalogError('shared_strings_malformed','共享字符串根元素不是 sst',{'part':part})
        declared=attr(root,'uniqueCount')
        if declared is not None:
            if not str(declared).isdigit():
                raise CatalogError('shared_strings_malformed','uniqueCount 不是非负整数',{'part':part},value=declared)
            self.shared_strings_declared=int(declared)
        for elem in root:
            if local_name(elem.tag)!='si':
                raise CatalogError('shared_strings_malformed','共享字符串含未知子元素',{'part':part},element=local_name(elem.tag))
            if len(self.shared_strings)>=self.package.limits.max_shared_strings:
                raise CatalogError('shared_strings_budget','共享字符串数量超过预算',{'part':part},limit=self.package.limits.max_shared_strings)
            self.shared_strings.append(self._rich_text(elem,part))
        if self.shared_strings_declared is not None and self.shared_strings_declared!=len(self.shared_strings):
            raise CatalogError('shared_strings_count_mismatch','uniqueCount 与实际字符串数量不一致',{'part':part},
                               declared=self.shared_strings_declared,actual=len(self.shared_strings))

    @staticmethod
    def _rich_text(container,part):
        pieces=[]
        for child in container:
            name=local_name(child.tag)
            if name=='t':pieces.append(child.text or '')
            elif name=='r':
                for grandchild in child:
                    if local_name(grandchild.tag)=='t':pieces.append(grandchild.text or '')
                    elif local_name(grandchild.tag)=='rPr':continue
                    else:raise CatalogError('shared_strings_malformed','富文本 run 含未知子元素',{'part':part},element=local_name(grandchild.tag))
            elif name in ('rPh','phoneticPr'):continue
            else:raise CatalogError('shared_strings_malformed','共享字符串含未知子元素',{'part':part},element=name)
        return ''.join(pieces)

    def layout_identity(self):
        """只读部件图与表对象，不读行；用于版本更换后的重新定位比对。"""
        tables=[]
        for sheet in self.sheets:
            for part,root in self.sheet_tables(sheet):
                definition=self.table_definition(part,root,sheet)
                tables.append({'name':attr(root,'name'),'display_name':attr(root,'displayName'),
                               'sheet_name':sheet['name'],'sheet_part':sheet['part'],
                               'sheet_state':sheet['state'],'table_part':part,
                               'ref':definition['ref'],
                               'columns':[{'name':column['name'],'id':column['id']}
                                          for column in definition['columns']]})
        return {'workbook_part':self.workbook_part,
                'date_system':'1904' if self.date_1904 else '1900',
                'sheets':[{'name':sheet['name'],'part':sheet['part'],'state':sheet['state']}
                          for sheet in self.sheets],
                'tables':tables,
                'external_links':self.external_links,
                'macros':self.macro_part is not None,
                'macro_part':self.macro_part}

    def _scan_auxiliary_relationships(self):
        self.external_links=[];self.macro_part=None
        _,rels=self.package.related(self.workbook_part)
        for rel in rels:
            if rel.kind in ('externalLink','externalLinkPath'):
                # 只登记，不解析、不跟随；外链目标可能越出包外。
                self.external_links.append({'kind':rel.kind,'target':rel.target,'external':rel.external})
            elif rel.kind=='vbaProject':
                # 只登记，不读取或执行宏部件；部件缺失不阻断名录定位。
                self.macro_part={'target':rel.target,'external':rel.external,
                                 'present':(not rel.external) and self.package.exists(self._resolve_quiet(rel))}
        self._table_cache={}

    def _resolve_quiet(self,rel):
        try:return self.package.resolve(rel)
        except CatalogError:return None

    def _load_styles(self):
        self.style_map={}
        self.styles_part=None
        rel=self.package.find_related(self.workbook_part,'styles')
        if rel is None:return
        part=self.package.resolve(rel)
        if not _is_content_type(self.content_type(part),_STYLES_CONTENT_TYPES):
            raise CatalogError('styles_content_type','样式部件内容类型不符',{'part':part},content_type=self.content_type(part))
        self.styles_part=part
        root=self.package.xml_root(part)
        if local_name(root.tag)!='styleSheet':raise CatalogError('styles_malformed','样式部件根元素不是 styleSheet',{'part':part})
        custom={};xfs=[]
        for elem in root:
            name=local_name(elem.tag)
            if name=='numFmts':
                for num_fmt in elem:
                    if local_name(num_fmt.tag)!='numFmt':
                        raise CatalogError('styles_malformed','numFmts 含未知子元素',{'part':part},element=local_name(num_fmt.tag))
                    fmt_id=attr(num_fmt,'numFmtId');code=attr(num_fmt,'formatCode')
                    if fmt_id is None or not str(fmt_id).isdigit():
                        raise CatalogError('styles_malformed','numFmt 缺少合法 numFmtId',{'part':part},numFmtId=fmt_id)
                    if code is None:
                        raise CatalogError('styles_malformed','numFmt 缺少 formatCode',{'part':part},numFmtId=fmt_id)
                    fmt_id=int(fmt_id)
                    if fmt_id in custom:
                        raise CatalogError('styles_malformed','numFmtId 重复',{'part':part},numFmtId=fmt_id)
                    custom[fmt_id]=code
            elif name=='cellXfs':
                count=attr(elem,'count')
                if count is not None and not str(count).isdigit():
                    raise CatalogError('styles_malformed','cellXfs count 不是整数',{'part':part},count=count)
                for xf in elem:
                    if local_name(xf.tag)!='xf':
                        raise CatalogError('styles_malformed','cellXfs 含未知子元素',{'part':part},element=local_name(xf.tag))
                    num_fmt=attr(xf,'numFmtId')
                    if num_fmt is not None and not str(num_fmt).isdigit():
                        raise CatalogError('styles_malformed','xf 的 numFmtId 不是整数',{'part':part},numFmtId=num_fmt)
                    xfs.append(None if num_fmt is None else int(num_fmt))
                if count is not None and int(count)!=len(xfs):
                    raise CatalogError('styles_malformed','cellXfs count 与 xf 数量不一致',{'part':part},declared=int(count),actual=len(xfs))
            elif name in ('fonts','fills','borders','cellStyleXfs','cellStyles','dxfs','tableStyles','colors','extLst'):
                continue
            else:
                raise CatalogError('styles_malformed','样式部件含未知元素',{'part':part},element=name)
        for index,num_id in enumerate(xfs):
            if num_id is None:self.style_map[index]=(None,None)
            elif num_id in custom:self.style_map[index]=(custom[num_id],num_id)
            else:self.style_map[index]=(None,num_id)

    def select_sheet(self,name):
        for sheet in self.sheets:
            if sheet['name']==name:return sheet
        return None

    def locate_table(self,table_name):
        """返回表对象、所属工作表、范围与列身份。"""
        if not table_name:raise CatalogError('table_name_required','来源配置缺少 table_name',{'workbook_part':self.workbook_part})
        matches=[];count=0;seen_parts={}
        for sheet in self.sheets:
            for part,root in self.sheet_tables(sheet):
                count+=1
                if part in seen_parts:
                    raise CatalogError('table_referenced_twice','同一表对象部件被多个工作表引用，归属不唯一',
                                       {'workbook_part':self.workbook_part,'table_part':part,
                                        'sheet_names':[seen_parts[part],sheet['name']]})
                seen_parts[part]=sheet['name']
                if count>self.package.limits.max_tables:
                    raise CatalogError('tables_budget','表对象数量超过预算',{'workbook_part':self.workbook_part},limit=self.package.limits.max_tables)
                if attr(root,'name')==table_name:
                    matches.append((sheet,part,root))
        if not matches:
            raise CatalogError('table_missing','工作簿中没有已登记名称的表对象',
                               {'workbook_part':self.workbook_part,'table_name':table_name},
                               sheets=[sheet['name'] for sheet in self.sheets])
        if len(matches)>1:
            raise CatalogError('table_duplicate','同一表名对应多个表对象，拒绝挑选',
                               {'workbook_part':self.workbook_part,'table_name':table_name},
                               matches=[{'sheet_name':sheet['name'],'table_part':part,'ref':attr(root,'ref')} for sheet,part,root in matches])
        sheet,part,root=matches[0]
        definition=self.table_definition(part,root,sheet)
        location={'workbook_part':self.workbook_part,'sheet_name':sheet['name'],'sheet_part':sheet['part'],
                  'table_part':part,'table_name':table_name}
        display=attr(root,'displayName')
        if display is not None and display!=table_name:
            location['table_display_name']=display
        return {'sheet':sheet,'part':part,'root':root,'ref':definition['ref'],
                'bounds':definition['bounds'],'columns':definition['columns'],
                'header_row_count':definition['header_row_count'],
                'totals_row_count':definition['totals_row_count'],
                'location':location}

    def sheet_tables(self,sheet):
        """按 worksheet→tableParts 关系读取；一次工作表只解析一次。"""
        cached=self._table_cache.get(sheet['part'])
        if cached is not None:return cached
        from .sheet import scan_table_parts
        location={'part':sheet['part'],'sheet_name':sheet['name']}
        count,rel_ids=scan_table_parts(self.package,sheet['part'],location)
        result=[]
        if rel_ids:
            _,rels=self.package.related(sheet['part'],'table')
            by_id={rel.id:rel for rel in rels}
            for rel_id in rel_ids:
                rel=by_id.get(rel_id)
                if rel is None:
                    raise CatalogError('table_relationship_missing','tablePart 的关系 Id 未在工作表关系中登记',
                                       {**location,'relationship_id':rel_id})
                part=self.package.resolve(rel)
                content=self.content_type(part)
                if not _is_content_type(content,_TABLE_CONTENT_TYPES):
                    raise CatalogError('table_content_type','表对象部件内容类型不符',{**location,'part':part},content_type=content)
                table_root=self.package.xml_root(part)
                if local_name(table_root.tag)!='table':
                    raise CatalogError('table_malformed','表对象根元素不是 table',{**location,'part':part})
                result.append((part,table_root))
        self._table_cache[sheet['part']]=result
        return result

    def table_definition(self,part,root,sheet):
        location={'table_part':part,'sheet_name':sheet['name'],'sheet_part':sheet['part'],'table_name':attr(root,'name')}
        ref=attr(root,'ref')
        r1,c1,r2,c2=parse_area(ref,location)
        header=attr(root,'headerRowCount')
        header_count=1 if header is None else int(header) if str(header).isdigit() else None
        if header is not None and header_count is None:
            raise CatalogError('table_malformed','headerRowCount 不是非负整数',location,value=header)
        totals=attr(root,'totalsRowCount')
        totals_count=0 if totals is None else int(totals) if str(totals).isdigit() else None
        if totals is not None and totals_count is None:
            raise CatalogError('table_malformed','totalsRowCount 不是非负整数',location,value=totals)
        columns_elem=None
        for child in root:
            name=local_name(child.tag)
            if name=='tableColumns':
                if columns_elem is not None:
                    raise CatalogError('table_malformed','表对象含多个 tableColumns',location)
                columns_elem=child
            elif name not in _TABLE_CHILDREN:
                raise CatalogError('table_malformed','表对象含未识别子元素',location,element=name)
        if columns_elem is None:raise CatalogError('table_columns_missing','表对象没有 tableColumns',location)
        count=attr(columns_elem,'count')
        columns=[]
        for column in columns_elem:
            if local_name(column.tag)!='tableColumn':
                raise CatalogError('table_columns_malformed','tableColumns 含未知子元素',location,element=local_name(column.tag))
            name=attr(column,'name')
            if not name:raise CatalogError('table_column_missing_name','tableColumn 缺少 name',location,column_index=len(columns)+1)
            computed=bool(attr(column,'totalsRowFunction') or attr(column,'calculatedColumnFormula'))
            for child in column:
                if local_name(child.tag) in ('calculatedColumnFormula','totalsRowFormula'):
                    computed=True
                elif local_name(child.tag)!='extLst':
                    raise CatalogError('table_columns_malformed','tableColumn 含未知子元素',
                                       {**location,'column_name':name},element=local_name(child.tag))
            columns.append({'name':name,'id':attr(column,'id'),'index':len(columns)+1,'computed':computed})
        if count is not None and (not str(count).isdigit() or int(count)!=len(columns)):
            raise CatalogError('table_columns_malformed','tableColumns count 与列数量不一致',location,declared=count,actual=len(columns))
        if len(columns)!=(c2-c1+1):
            raise CatalogError('table_range_conflict','表范围列数与 tableColumns 数量不一致',location,ref=ref,range_columns=c2-c1+1,declared_columns=len(columns))
        if header_count is None:header_count=1
        if header_count>1:
            raise CatalogError('table_header_rows_unsupported','headerRowCount 大于 1 的多行表头不在既定口径内',
                               location,header_row_count=header_count)
        if totals_count>1:
            raise CatalogError('table_totals_rows_unsupported','totalsRowCount 大于 1 不在既定口径内',
                               location,totals_row_count=totals_count)
        if header_count+totals_count>(r2-r1+1):
            raise CatalogError('table_range_conflict','表头与汇总行数超出表范围',location,ref=ref,
                               header_row_count=header_count,totals_row_count=totals_count)
        seen=set()
        for column in columns:
            if column['name'] in seen:
                raise CatalogError('table_column_duplicate','表列名重复，列身份不唯一',location,column_name=column['name'],ref=ref,column_index=column['index'])
            seen.add(column['name'])
        return {'ref':ref,'bounds':(r1,c1,r2,c2),'columns':columns,
                'header_row_count':header_count,'totals_row_count':totals_count}
