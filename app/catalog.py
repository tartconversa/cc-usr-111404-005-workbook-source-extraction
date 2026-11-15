"""在领取阶段解释单份响应：定位表对象、按列身份映射并保真读取记录。

入口接受 metadata（table_name、field_mapping、hidden_sheets、hidden_rows、
include_unmapped_columns）与读取预算；来源已从配置中删除的字段仍保留在
columns 中并给出未采原因。失败抛出 CatalogError，失败位置随错误返回。
"""
from .data import cell_ref
from .errors import CatalogError
from .ooxml import Limits, Package, attr
from .workbook import WorkbookLayout, check_media_type

_HIDDEN_SHEET_POLICIES={'include','exclude'}
_HIDDEN_ROW_POLICIES={'include','exclude'}

DEFAULT_CONFIG={
    'table_name':None,
    'field_mapping':{},
    'hidden_sheets':'include',
    'hidden_rows':'include',
    'include_unmapped_columns':False,
}


class SourceConfig:
    """来源侧配置的校验结果；配置错误在读取包之前就失败。"""

    def __init__(self,metadata,limits):
        if metadata is None:metadata={}
        if not isinstance(metadata,dict):raise CatalogError('config_invalid','来源 metadata 必须是对象',{'config':True})
        known=set(DEFAULT_CONFIG)|{'limits'}
        unknown=set(metadata)-known
        if unknown:
            raise CatalogError('config_invalid','来源 metadata 含未知配置项',{'config':True},keys=sorted(unknown))
        merged=dict(DEFAULT_CONFIG);merged.update({key:metadata[key] for key in metadata if key!='limits'})
        table_name=merged['table_name']
        if not isinstance(table_name,str) or not table_name.strip():
            raise CatalogError('table_name_required','来源配置缺少 table_name',{'config':True})
        self.table_name=table_name
        mapping=merged['field_mapping']
        if not isinstance(mapping,dict):
            raise CatalogError('config_invalid','field_mapping 必须是对象',{'config':True})
        target_keys={};field_to_column={}
        for column,field in mapping.items():
            if not isinstance(column,str) or not column:
                raise CatalogError('config_invalid','field_mapping 的列名必须是非空字符串',{'config':True},column=column)
            if not isinstance(field,str) or not field:
                raise CatalogError('config_invalid','field_mapping 的目标字段名必须是非空字符串',{'config':True},column=column,field=field)
            if field in target_keys:
                raise CatalogError('config_invalid','field_mapping 把多个列映射到同一目标字段',{'config':True},
                                   field=field,columns=[target_keys[field],column])
            if column in field_to_column:
                raise CatalogError('config_invalid','field_mapping 重复登记同一列',{'config':True},column=column)
            target_keys[field]=column;field_to_column[column]=field
        self.hidden_sheets=merged['hidden_sheets']
        if self.hidden_sheets not in _HIDDEN_SHEET_POLICIES:
            raise CatalogError('config_invalid','hidden_sheets 只能是 include 或 exclude',{'config':True},value=self.hidden_sheets)
        self.hidden_rows=merged['hidden_rows']
        if self.hidden_rows not in _HIDDEN_ROW_POLICIES:
            raise CatalogError('config_invalid','hidden_rows 只能是 include 或 exclude',{'config':True},value=self.hidden_rows)
        self.field_mapping={column:field for column,field in merged['field_mapping'].items()}
        self.include_unmapped_columns=merged['include_unmapped_columns']
        if type(self.include_unmapped_columns) is not bool:
            raise CatalogError('config_invalid','include_unmapped_columns 必须是布尔值',{'config':True})
        if isinstance(limits,SourceConfig):raise CatalogError('config_invalid','读取预算必须是 Limits 或对象',{'config':True})
        try:
            if isinstance(limits,dict):limits=Limits(**limits)
            if limits is None:limits=Limits()
            if not isinstance(limits,Limits):
                raise CatalogError('config_invalid','读取预算必须是 Limits',{'config':True})
            self.limits=limits.replace(metadata.get('limits'))
        except CatalogError:raise
        except (TypeError,ValueError) as exc:
            raise CatalogError('config_invalid','读取预算非法: '+str(exc),{'config':True}) from exc


class CatalogCollector:
    def __init__(self,response,metadata=None,limits=None):
        self.response=response
        self.config=SourceConfig(metadata,limits)
        self.media_type=check_media_type(response.headers)

    def collect(self,record_sink=None):
        """返回名录。提供 record_sink 时逐条交给调用方，不累积 rows。

        sink 与返回值分离后，大工作簿可以边读边写，内存不随记录数增长；
        返回值仍保留行数、列身份、未采原因与读取统计。
        """
        if record_sink is not None and not callable(record_sink):
            raise CatalogError('config_invalid','record_sink 必须可调用',{'config':True})
        with Package(self.response.body,self.config.limits) as package:
            layout=WorkbookLayout(package)
            table=layout.locate_table(self.config.table_name)
            collected=self._read(layout,table,record_sink=record_sink)
            result={
                'source':{'source_id':self.response.source_id,'url':self.response.url,
                          'status':self.response.status,'response_sha256':self.response.sha256},
                'workbook':{'media_type':self.media_type,'archive_bytes':package.archive_bytes,
                            'archive_sha256':self.response.sha256,
                            'workbook_part':layout.workbook_part,
                            'date_system':'1904' if layout.date_1904 else '1900',
                            'shared_strings_part':layout.shared_strings_part,
                            'styles_part':layout.styles_part,
                            'external_links':list(layout.external_links),
                            'macro_part':layout.macro_part,
                            'calculation':{'calc_pr':layout.calc_pr,
                                           'macros_executed':False,'external_links_followed':False,
                                           'recalculated':False}},
                'table':{'name':self.config.table_name,
                         'display_name':attr(table['root'],'displayName'),
                         'sheet_name':table['sheet']['name'],
                         'sheet_state':table['sheet']['state'],
                         'sheet_order':table['sheet']['order'],
                         'sheet_visible':table['sheet']['state']=='visible',
                         'worksheet_part':table['sheet']['part'],
                         'table_part':table['part'],
                         'ref':table['ref'],
                         'bounds':{'first_row':table['bounds'][0],'first_column':table['bounds'][1],
                                   'last_row':table['bounds'][2],'last_column':table['bounds'][3]},
                         'header_row_count':table['header_row_count'],
                         'totals_row_count':table['totals_row_count']},
                'columns':collected['columns'],
                'missing_configured_columns':collected['missing_configured_columns'],
                'row_stats':collected['row_stats'],
                'field_status':collected['field_status'],
                'config':{'table_name':self.config.table_name,'field_mapping':dict(self.config.field_mapping),
                          'hidden_sheets':self.config.hidden_sheets,'hidden_rows':self.config.hidden_rows,
                          'include_unmapped_columns':self.config.include_unmapped_columns,
                          'limits':self.config.limits.as_dict()},
            }
            if record_sink is None:result['rows']=collected['rows']
            else:result['rows_streamed']=collected['rows_streamed']
            return result

    def _read(self,layout,table,record_sink=None):
        from .sheet import RowScan
        r1,c1,r2,c2=table['bounds']
        sheet=table['sheet']
        if sheet['state']!='visible' and self.config.hidden_sheets=='exclude':
            raise CatalogError('sheet_hidden_excluded','目标表所在工作表为隐藏状态，来源配置要求排除隐藏表',
                               {**table['location'],'sheet_state':sheet['state']})
        decoder=_decoder(layout)
        mapping=self.config.field_mapping
        table_columns=table['columns']
        table_names={column['name']:column for column in table_columns}
        missing_configured=[]
        for column_name,field in mapping.items():
            if column_name not in table_names:
                missing_configured.append({'column':column_name,'field':field,'reason':'configured_column_not_in_table',
                                           'detail':'来源 field_mapping 登记的列在目标表对象中不存在，未采该字段'})
        mapped_columns={name for name in mapping if name in table_names}
        column_info=[];field_positions={}
        for column in table_columns:
            field=mapping.get(column['name'])
            entry={'name':column['name'],'table_column_id':column['id'],'index':column['index'],
                   'field':field,'sheet_column':column['index']+c1-1,
                   'sheet_column_letters':self._letters(column['index']+c1-1),
                   'formula_column':bool(column.get('computed')),'population':None}
            if field is None:
                entry['not_collected']={'reason':'not_mapped',
                                        'detail':'该表列未在来源 field_mapping 中映射，仅在 include_unmapped_columns 为真时随记录保留原值'}
            column_info.append(entry)
            if field is not None:field_positions[field]=(column,entry)
        header_count=table['header_row_count']
        totals_count=table['totals_row_count']
        data_first=r1+header_count
        data_last=r2-totals_count
        records=[];records_emitted=0
        hidden_included=0;hidden_skipped=0;blank=0;cells_read=0
        populations={column['name']:0 for column in table_columns}
        scan=RowScan(layout.package,sheet['part'],{'part':sheet['part'],'sheet_name':sheet['name']},table['bounds'])
        seen_rows=[]
        header_seen=False
        for row in scan.run():
            seen_rows.append(row['row_number'])
            row_number=row['row_number']
            if header_count>=1 and row_number==r1:
                _verify_headers(decoder,row,table,c1)
                header_seen=True
            if row_number<data_first or row_number>data_last:
                continue
            if row['hidden']:
                if self.config.hidden_rows=='exclude':
                    hidden_skipped+=1
                    continue
                hidden_included+=1
            cells={}
            populated=False
            for column in table_columns:
                sheet_column=column['index']+c1-1
                raw_cell=row['cells'].get(sheet_column)
                location={'part':sheet['part'],'sheet_name':sheet['name'],'row':row_number,
                          'cell':cell_ref(row_number,sheet_column),'table_part':table['part']}
                cells_read+=1
                if cells_read>layout.package.limits.max_cells:
                    raise CatalogError('cells_budget','解释的单元格数量超过预算',location,
                                       limit=layout.package.limits.max_cells)
                cell_record=decoder.decode(raw_cell,location)
                cells[column['name']]=cell_record
                if not _is_without_value(cell_record):
                    populations[column['name']]+=1
                    populated=True
            if not populated:blank+=1
            record={'row_number':row_number,'hidden':row['hidden'],'values':{}}
            for field,(column,entry) in field_positions.items():
                cell_record=cells[column['name']]
                projected=_project(cell_record)
                projected['column']=column['name']
                projected['cell']=cell_ref(row_number,entry['sheet_column'])
                record['values'][field]=projected
            if self.config.include_unmapped_columns:
                record['unmapped']={}
                for name,cell in cells.items():
                    if name in mapped_columns:continue
                    projected=_project(cell)
                    projected['cell']=cell_ref(row_number,table_names[name]['index']+c1-1)
                    record['unmapped'][name]=projected
            if record_sink is None:records.append(record)
            else:
                record_sink(record)
                records_emitted+=1
        if header_count>=1 and not header_seen:
            raise CatalogError('table_header_missing','表头行不在工作表中，无法确认列身份',
                               {**table['location'],'header_row':r1})
        out_of_order=[seen_rows[index] for index in range(1,len(seen_rows)) if seen_rows[index]<seen_rows[index-1]]
        if record_sink is None:records.sort(key=lambda item:item['row_number'])
        for entry in column_info:
            entry['population']=populations[entry['name']]
        missing_rows=[number for number in range(r1,r2+1) if number not in seen_rows]
        emitted=len(records) if record_sink is None else records_emitted
        row_stats={'declared_rows':r2-r1+1,
                   'data_rows':max(0,data_last-data_first+1),
                   'records':emitted,'blank_rows':blank,
                   'hidden_rows_included':hidden_included,'hidden_rows_skipped':hidden_skipped,
                   'hidden_row_policy':self.config.hidden_rows,
                   'missing_rows':missing_rows,'missing_row_count':len(missing_rows),
                   'out_of_order_rows':out_of_order,
                   'row_order':'coordinate' if record_sink is None else 'document',
                   'outside_table_cells':scan.outside_cells}
        field_status=[]
        for field,(column,entry) in field_positions.items():
            field_status.append({'field':field,'column':column['name'],'collected':True})
        for missing in missing_configured:
            field_status.append({'field':missing['field'],'column':missing['column'],'collected':False,
                                 'reason':missing['reason']})
        result={'rows':records,'columns':column_info,'missing_configured_columns':missing_configured,
                'row_stats':row_stats,'field_status':field_status}
        if record_sink is not None:result['rows_streamed']=row_stats['records']
        return result

    @staticmethod
    def _letters(index):
        from .data import column_letters
        return column_letters(index)


def _decoder(layout):
    from .cells import CellDecoder
    return CellDecoder(layout.shared_strings,layout.style_map,1904 if layout.date_1904 else 1900)


def _verify_headers(decoder,row,table,first_column):
    sheet=table['sheet']
    header_row=row['row_number']
    for column in table['columns']:
        sheet_column=column['index']+first_column-1
        raw_cell=row['cells'].get(sheet_column)
        location={'part':sheet['part'],'sheet_name':sheet['name'],'row':header_row,
                  'cell':cell_ref(header_row,sheet_column),'table_part':table['part']}
        record=decoder.decode(raw_cell,location)
        text=(record.get('text') or '')
        if text!=column['name']:
            raise CatalogError('table_header_conflict','表列身份与工作表表头单元格不一致',
                               location,table_column=column['name'],header_value=record.get('text'),
                               header_state=record.get('state'))


def _is_without_value(cell_record):
    return cell_record['state'] in ('missing_cell','empty_value')


def _project(cell_record):
    interpretation=cell_record.get('interpretation')
    state=cell_record['state']
    if interpretation is not None:value=interpretation
    elif state in ('text','empty_string','boolean'):value=cell_record.get('text')
    elif state=='error':value=cell_record.get('raw')
    else:value=None
    projected={'state':state,'status':cell_record['status'],
               'raw':cell_record.get('raw'),'value':value,
               'text':cell_record.get('text'),'notes':list(cell_record.get('notes') or [])}
    if 'number' in cell_record:projected['number']=cell_record['number']
    if 'shared_index' in cell_record:projected['shared_index']=cell_record['shared_index']
    if 'display' in cell_record:projected['display']=cell_record['display']
    if 'format' in cell_record:
        fmt=cell_record['format']
        projected['format']={'classification':fmt['classification'],'format_code':fmt['format_code'],
                             'format_id':fmt['format_id'],'interpreted':fmt['classification']!='unknown'}
        if 'zeros' in fmt:projected['format']['zeros']=fmt['zeros']
    if 'formula' in cell_record:projected['formula']=cell_record['formula']
    if cell_record.get('formula_meta'):projected['formula']['meta']=cell_record['formula_meta']
    if 'serial' in cell_record:projected['serial']=cell_record['serial']
    if 'shared_index' in cell_record:projected['shared_index']=cell_record['shared_index']
    return projected


def collect_response(response,metadata=None,limits=None,record_sink=None):
    return CatalogCollector(response,metadata,limits).collect(record_sink=record_sink)


def inspect_layout(response,limits=None):
    """只领取包结构、工作表与表对象，不读取数据行；用于换版后重新定位。"""
    with Package(response.body,limits or Limits()) as package:
        return WorkbookLayout(package).layout_identity()


def relocalise(previous,current,table_name):
    """比较两次 layout_identity，说明目标表是否仍可按列身份定位。

    返回 ok、reason 与差异；不读取或合并业务记录，也不把“仍在同名工作表
    的同一偏移”当作等价，只有 table/@name 与列名可比。
    """
    if not isinstance(previous,dict) or not isinstance(current,dict):
        raise CatalogError('relocalise_invalid','重新定位需要两份 layout_identity',{'config':True})
    def pick(identity):
        for table in identity.get('tables',[]):
            if table.get('name')==table_name:return table
        return None
    before=pick(previous);after=pick(current)
    problems=[]
    if before is None:problems.append({'reason':'previous_table_missing','detail':'上一版本没有该表对象'})
    if after is None:problems.append({'reason':'current_table_missing','detail':'当前版本没有该表对象'})
    if problems:return {'ok':False,'table_name':table_name,'problems':problems}
    old_columns=[column['name'] for column in before['columns']]
    new_columns=[column['name'] for column in after['columns']]
    if old_columns!=new_columns:
        missing=[name for name in old_columns if name not in new_columns]
        added=[name for name in new_columns if name not in old_columns]
        if missing:problems.append({'reason':'columns_removed','columns':missing,
                                    'detail':'列身份消失，相关字段无法按名继续领取'})
        if added:problems.append({'reason':'columns_added','columns':added,
                                  'detail':'新列未登记，不会自动进入字段映射'})
        if not missing and old_columns!=new_columns:
            problems.append({'reason':'column_order_changed','before':old_columns,'after':new_columns,
                             'detail':'列顺序改变不影响按列身份映射'})
    moved=before['sheet_name']!=after['sheet_name'] or before['sheet_part']!=after['sheet_part']
    return {'ok':not any(p['reason'] in ('columns_removed','previous_table_missing','current_table_missing') for p in problems),
            'table_name':table_name,
            'previous':{'sheet_name':before['sheet_name'],'table_part':before['table_part'],'ref':before['ref'],'columns':old_columns},
            'current':{'sheet_name':after['sheet_name'],'table_part':after['table_part'],'ref':after['ref'],'columns':new_columns},
            'relocated':moved or before['ref']!=after['ref'],
            'problems':problems}

