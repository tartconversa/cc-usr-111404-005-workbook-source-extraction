"""名录采集的合成工作簿测试：定位、列身份、类型与失败位置。

不连接真实来源；所有工作簿在内存中按 OOXML 最小组装，固定关系链，
只覆盖本仓库契约中的行为。
"""
import base64,io,unittest,warnings,zipfile
from xml.sax.saxutils import escape

from app.catalog import collect_response,inspect_layout,relocalise
from app.errors import CatalogError
from app.ooxml import Limits
from app.source import from_packet

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P='http://schemas.openxmlformats.org/package/2006/relationships'
CT='http://schemas.openxmlformats.org/package/2006/content-types'
CT_SHEET='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'
CT_TABLE='application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml'
CT_WB='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
CT_SS='application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml'
CT_ST='application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml'
MEDIA='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def inline(ref,text,style=None):
    s='' if style is None else ' s="%s"'%style
    return '<c r="%s" t="inlineStr"%s><is><t>%s</t></is></c>'%(ref,s,escape(text))


def num(ref,text,style=None):
    s='' if style is None else ' s="%s"'%style
    return '<c r="%s"%s><v>%s</v></c>'%(ref,s,text)


def row(number,cells,hidden=False):
    return '<row r="%d"%s>%s</row>'%(number,' hidden="1"' if hidden else '',cells)


def sheet_xml(rows):
    return '<worksheet xmlns="%s" xmlns:r="%s"><sheetData>%s</sheetData></worksheet>'%(NS,R,rows)


def table_xml(name,ref,columns,display=None):
    cols=''.join('<tableColumn id="%d" name="%s"/>'%(i+1,escape(column)) for i,column in enumerate(columns))
    return '<table xmlns="%s" id="1" name="%s" displayName="%s" ref="%s"><tableColumns count="%d">%s</tableColumns></table>'%(NS,escape(name),escape(display or name),ref,len(columns),cols)


def package(sheets,shared=None,styles=None,date1904=False,sheet_rels=None,mutate=None,extra=None,workbook_rels=None,workbook_part=None):
    """sheets: [{'name','state','xml','tables':[(part,bytes)]}]。"""
    parts={};wb_sheets=[];wb_rels=[]
    overrides=[('xl/workbook.xml',CT_WB)]
    for index,sheet in enumerate(sheets,1):
        part='xl/worksheets/sheet%d.xml'%index
        xml=sheet['xml'];trels=[]
        if sheet.get('tables'):
            table_parts=[]
            for position,(table_part,payload) in enumerate(sheet['tables'],1):
                parts[table_part]=payload
                overrides.append((table_part,CT_TABLE))
                rid='t%d_%d'%(index,position)
                table_parts.append('<tablePart r:id="%s"/>'%rid)
                trels.append('<Relationship Id="%s" Type="%s/table" Target="../tables/%s"/>'%(rid,R,table_part.rsplit('/',1)[1]))
            xml=xml.replace('</sheetData></worksheet>','</sheetData><tableParts count="%d">%s</tableParts></worksheet>'%(len(table_parts),''.join(table_parts)))
        parts[part]=xml;overrides.append((part,CT_SHEET))
        state='' if not sheet.get('state') else ' state="%s"'%sheet['state']
        wb_sheets.append('<sheet name="%s" sheetId="%d" r:id="r%d"%s/>'%(escape(sheet['name']),index,index,state))
        wb_rels.append('<Relationship Id="r%d" Type="%s/worksheet" Target="worksheets/sheet%d.xml"/>'%(index,R,index))
        if sheet_rels is not None and index==1:
            parts['xl/worksheets/_rels/sheet1.xml.rels']=sheet_rels
        elif trels:
            parts['xl/worksheets/_rels/sheet%d.xml.rels'%index]='<Relationships xmlns="%s">%s</Relationships>'%(P,''.join(trels))
    if shared is not None:
        parts['xl/sharedStrings.xml']=shared
        overrides.append(('xl/sharedStrings.xml',CT_SS))
        wb_rels.append('<Relationship Id="ss" Type="%s/sharedStrings" Target="sharedStrings.xml"/>'%R)
    if styles is not None:
        parts['xl/styles.xml']=styles
        overrides.append(('xl/styles.xml',CT_ST))
        wb_rels.append('<Relationship Id="sty" Type="%s/styles" Target="styles.xml"/>'%R)
    overrides_xml=''.join('<Override PartName="/%s" ContentType="%s"/>'%(p,c) for p,c in overrides)
    parts['[Content_Types].xml']='<Types xmlns="%s"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>%s</Types>'%(CT,overrides_xml)
    parts['_rels/.rels']='<Relationships xmlns="%s"><Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.xml"/></Relationships>'%(P,R)
    parts['xl/workbook.xml']='<workbook xmlns="%s" xmlns:r="%s"><workbookPr date1904="%d"/><calcPr calcId="191029"/><sheets>%s</sheets></workbook>'%(NS,R,1 if date1904 else 0,''.join(wb_sheets))
    if workbook_rels is not None:wb_rels=list(wb_rels)+list(workbook_rels)
    parts['xl/_rels/workbook.xml.rels']='<Relationships xmlns="%s">%s</Relationships>'%(P,''.join(wb_rels))
    if workbook_part:parts['xl/workbook.xml']=workbook_part
    parts.update(extra or {})
    if mutate:mutate(parts)
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,payload in parts.items():archive.writestr(name,payload)
    return buffer.getvalue()


def response(body,media=MEDIA):
    packet={'source_id':'test','url':'https://example.test/source','status':200,
            'headers':[['Content-Type',media]],'body_base64':base64.b64encode(body).decode()}
    return from_packet(packet)


def collect(body,metadata,limits=None):
    return collect_response(response(body),metadata,limits)


BASE_ROWS=row(1,inline('A1','编号')+inline('B1','日期')+inline('C1','备注'))
BASE_TABLE=( 'xl/tables/table1.xml',table_xml('Devices','A1:C3',['编号','日期','备注']).encode() )


def basic(rows=None,table=None,styles=None,**kwargs):
    cells=rows if rows is not None else BASE_ROWS+row(2,num('A2','1')+num('B2','2'))
    table=table or table_xml('Devices','A1:C3',['编号','日期','备注'])
    return package([{'name':'设备','xml':sheet_xml(cells),'tables':[('xl/tables/table1.xml',table.encode())]}],
                   styles=styles,**kwargs)


class LocateTests(unittest.TestCase):
    def test_table_located_by_name_after_sheet_and_filename_changes(self):
        rows=BASE_ROWS+row(2,num('A2','7'))
        before=basic(rows=rows,table=table_xml('Devices','A1:C3',['编号','日期','备注']))
        after=package([{'name':'说明','xml':sheet_xml(row(1,inline('A1','x'))),'tables':[]},
                       {'name':'主设备','xml':sheet_xml(rows),'tables':[('xl/tables/table9.xml',table_xml('Devices','A1:C3',['编号','日期','备注']).encode())]}])
        previous=inspect_layout(response(before));current=inspect_layout(response(after))
        report=relocalise(previous,current,'Devices')
        self.assertTrue(report['ok'])
        self.assertEqual(report['current']['sheet_name'],'主设备')
        result=collect(after,{'table_name':'Devices','field_mapping':{'编号':'id'}})
        self.assertEqual(result['table']['sheet_name'],'主设备')
        self.assertEqual(result['rows'][0]['values']['id']['value'],7)

    def test_relocalise_reports_removed_column(self):
        previous=inspect_layout(response(basic()))
        current=inspect_layout(response(basic(table=table_xml('Devices','A1:B3',['编号','日期']))))
        report=relocalise(previous,current,'Devices')
        self.assertFalse(report['ok'])
        self.assertEqual(report['problems'][0]['reason'],'columns_removed')
        self.assertIn('备注',report['problems'][0]['columns'])

    def test_missing_table_keeps_location(self):
        body=basic(table=table_xml('Other','A1:C3',['编号','日期','备注']))
        with self.assertRaises(CatalogError) as caught:
            collect(body,{'table_name':'Devices','field_mapping':{'编号':'id'}})
        self.assertEqual(caught.exception.code,'table_missing')
        self.assertEqual(caught.exception.location['workbook_part'],'/xl/workbook.xml')
        self.assertEqual(caught.exception.location['table_name'],'Devices')

    def test_duplicate_table_name_rejected(self):
        table=table_xml('Devices','A1:C3',['编号','日期','备注'])
        body=package([{'name':'甲','xml':sheet_xml(BASE_ROWS),'tables':[('xl/tables/a.xml',table.encode())]},
                      {'name':'乙','xml':sheet_xml(BASE_ROWS),'tables':[('xl/tables/b.xml',table.encode())]}])
        with self.assertRaises(CatalogError) as caught:
            collect(body,{'table_name':'Devices','field_mapping':{}})
        self.assertEqual(caught.exception.code,'table_duplicate')
        self.assertEqual(len(caught.exception.details['matches']),2)

    def test_range_conflict_and_header_conflict(self):
        body=basic(table=table_xml('Devices','A1:D3',['编号','日期','备注']))
        with self.assertRaises(CatalogError) as caught:
            collect(body,{'table_name':'Devices'})
        self.assertEqual(caught.exception.code,'table_range_conflict')
        rows=BASE_ROWS.replace('编号','代码',1)+row(2,num('A2','1'))
        body=basic(rows=rows)
        with self.assertRaises(CatalogError) as caught:
            collect(body,{'table_name':'Devices','field_mapping':{'编号':'id'}})
        self.assertEqual(caught.exception.code,'table_header_conflict')
        self.assertEqual(caught.exception.location['cell'],'A1')
        self.assertEqual(caught.exception.location['table_part'],'/xl/tables/table1.xml')

    def test_configured_column_missing_is_reported_not_dropped(self):
        body=basic()
        result=collect(body,{'table_name':'Devices','field_mapping':{'编号':'id','停产原因':'reason'}})
        self.assertEqual(result['missing_configured_columns'][0]['field'],'reason')
        self.assertFalse([entry for entry in result['field_status'] if entry['field']=='reason'][0]['collected'])
        unmapped=[column for column in result['columns'] if column['name']=='备注'][0]
        self.assertEqual(unmapped['not_collected']['reason'],'not_mapped')


class ValueTests(unittest.TestCase):
    STYLES='<styleSheet xmlns="%s"><numFmts count="2"><numFmt numFmtId="164" formatCode="000000"/><numFmt numFmtId="165" formatCode="yyyy-mm-dd"/></numFmts><cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="164"/><xf numFmtId="165"/></cellXfs></styleSheet>'%NS

    def values(self,rows,table=None,date1904=False,columns=None):
        columns=columns or ['编号','日期','备注']
        rows=BASE_ROWS+rows
        body=basic(rows=rows,table=table or table_xml('Devices','A1:C4',columns),styles=self.STYLES,date1904=date1904)
        result=collect(body,{'table_name':'Devices','field_mapping':{'编号':'id','日期':'date','备注':'note'}})
        return result

    def test_leading_zero_text_stays_text_and_zero_padded_number_gets_display(self):
        result=self.values(row(2,inline('A2','0012')+num('B2','44927',2)+inline('C2','')))
        record=result['rows'][0]['values']
        self.assertEqual(record['id']['state'],'text')
        self.assertEqual(record['id']['value'],'0012')
        self.assertEqual(record['date']['value'],'2023-01-01')
        self.assertEqual(record['note']['state'],'empty_string')
        self.assertEqual(record['note']['value'],'')
        result=self.values(row(2,num('A2','12',1)+num('B2','1')+inline('C2','x')))
        id_value=result['rows'][0]['values']['id']
        self.assertEqual((id_value['state'],id_value['number'],id_value['display']),('number',12,'000012'))

    def test_missing_cell_empty_value_and_error_stay_distinct(self):
        result=self.values(row(2,num('B2','1')+'<c r="C2" t="e"><v>#N/A</v></c>'))
        values=result['rows'][0]['values']
        self.assertEqual(values['id']['state'],'missing_cell')
        self.assertIsNone(values['id']['value'])
        self.assertEqual(values['note']['state'],'error')
        self.assertEqual(values['note']['value'],'#N/A')
        body=basic(rows=BASE_ROWS+row(2,'<c r="A2"><f>1+1</f></c>'),styles=self.STYLES)
        result=collect(body,{'table_name':'Devices','field_mapping':{'编号':'id'}})
        self.assertEqual(result['rows'][0]['values']['id']['state'],'formula_cache_missing')

    def test_formula_only_uses_cache(self):
        rows=BASE_ROWS+row(2,'<c r="A2"><f>A2*2</f><v>9</v></c>'+'<c r="B2" t="e"><f>1/0</f><v>#DIV/0!</v></c>')
        body=basic(rows=rows,styles=self.STYLES)
        result=collect(body,{'table_name':'Devices','field_mapping':{'编号':'id','日期':'date'}})
        values=result['rows'][0]['values']
        self.assertEqual(values['id']['formula']['expression'],'A2*2')
        self.assertTrue(values['id']['formula']['cache_present'])
        self.assertTrue(values['id']['formula']['cache_usable'])
        self.assertEqual(values['id']['value'],9)
        self.assertEqual(values['date']['state'],'formula_cache_present')
        self.assertFalse(values['date']['formula']['cache_usable'])
        self.assertEqual(values['date']['formula']['cache_error'],'#DIV/0!')

    def test_date_systems_and_fictitious_leap_day(self):
        result=self.values(row(2,num('A2','1')+num('B2','44927',2)+inline('C2','x')))
        self.assertEqual(result['workbook']['date_system'],'1900')
        self.assertEqual(result['rows'][0]['values']['date']['value'],'2023-01-01')
        result=self.values(row(2,num('A2','1')+num('B2','60',2)+inline('C2','x')))
        serial=result['rows'][0]['values']['date']
        self.assertEqual(serial['state'],'not_interpreted')
        self.assertIsNone(serial['value'])
        self.assertIn('虚构闰日',serial['notes'][0])
        result=self.values(row(2,num('A2','1')+num('B2','1',2)+inline('C2','x')),date1904=True)
        self.assertEqual(result['workbook']['date_system'],'1904')
        self.assertEqual(result['rows'][0]['values']['date']['value'],'1904-01-02')

    def test_unknown_format_is_kept_uninterpreted(self):
        unknown='<styleSheet xmlns="%s"><numFmts count="1"><numFmt numFmtId="166" formatCode="&quot;kg&quot;"/></numFmts><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="166"/></cellXfs></styleSheet>'%NS
        rows=BASE_ROWS+row(2,num('A2','5',1)+inline('B2','x'))
        body=basic(rows=rows,table=table_xml('Devices','A1:C2',['编号','日期','备注']),styles=unknown)
        result=collect(body,{'table_name':'Devices','field_mapping':{'编号':'id'}})
        value=result['rows'][0]['values']['id']
        self.assertEqual(value['state'],'number')
        self.assertFalse(value['format']['interpreted'])
        self.assertIsNone(value['display'])


class AccessTests(unittest.TestCase):
    def test_hidden_row_policy_is_explicit_not_deletion(self):
        rows=BASE_ROWS+row(2,num('A2','1'),hidden=True)+row(3,num('A3','2'))
        body=basic(rows=rows,table=table_xml('Devices','A1:C3',['编号','日期','备注']))
        included=collect(body,{'table_name':'Devices','field_mapping':{'编号':'id'}})
        self.assertEqual([(r['row_number'],r['hidden']) for r in included['rows']],[(2,True),(3,False)])
        excluded=collect(body,{'table_name':'Devices','field_mapping':{'编号':'id'},'hidden_rows':'exclude'})
        self.assertEqual([r['row_number'] for r in excluded['rows']],[3])
        self.assertEqual(excluded['row_stats']['hidden_rows_skipped'],1)

    def test_hidden_sheet_policy_from_configuration(self):
        rows=BASE_ROWS+row(2,num('A2','5'))
        body=package([{'name':'设备','state':'hidden','xml':sheet_xml(rows),
                       'tables':[BASE_TABLE]}])
        included=collect(body,{'table_name':'Devices','field_mapping':{'编号':'id'}})
        self.assertEqual(included['table']['sheet_state'],'hidden')
        with self.assertRaises(CatalogError) as caught:
            collect(body,{'table_name':'Devices','field_mapping':{'编号':'id'},'hidden_sheets':'exclude'})
        self.assertEqual(caught.exception.code,'sheet_hidden_excluded')

    def test_sparse_rows_and_outside_cells(self):
        rows=BASE_ROWS+'<row r="3"><c r="C3"><v>1</v></c></row><row r="2"><c r="C2"><v>2</v></c><c r="E2"><v>9</v></c></row>'
        body=basic(rows=rows,table=table_xml('Devices','A1:C4',['编号','日期','备注']))
        result=collect(body,{'table_name':'Devices','field_mapping':{'备注':'note'}})
        self.assertEqual([r['row_number'] for r in result['rows']],[2,3])
        self.assertEqual(result['row_stats']['missing_rows'],[4])
        self.assertEqual(result['row_stats']['outside_table_cells'],1)
        self.assertEqual(result['row_stats']['out_of_order_rows'],[2])
        self.assertEqual(result['row_stats']['row_order'],'coordinate')

    def test_duplicate_row_and_cell_rejected(self):
        rows=BASE_ROWS+row(2,num('A2','1'))+row(2,num('A2','2'))
        body=basic(rows=rows)
        with self.assertRaises(CatalogError) as caught:
            collect(body,{'table_name':'Devices','field_mapping':{'编号':'id'}})
        self.assertEqual(caught.exception.code,'row_duplicate')
        self.assertEqual(caught.exception.location['row'],2)
        rows=BASE_ROWS+'<row r="2">'+num('A2','1')+num('A2','2')+'</row>'
        with self.assertRaises(CatalogError) as caught:
            collect(basic(rows=rows),{'table_name':'Devices','field_mapping':{'编号':'id'}})
        self.assertEqual(caught.exception.code,'cell_duplicate')

    def test_relationship_targets_outside_package_rejected(self):
        external='<Relationships xmlns="%s"><Relationship Id="t1_1" Type="%s/table" Target="https://evil.test/t.xml" TargetMode="External"/></Relationships>'%(P,R)
        body=basic(sheet_rels=external)
        with self.assertRaises(CatalogError) as caught:
            collect(body,{'table_name':'Devices'})
        self.assertEqual(caught.exception.code,'relationship_external')
        traversal='<Relationships xmlns="%s"><Relationship Id="t1_1" Type="%s/table" Target="../../../../etc/passwd"/></Relationships>'%(P,R)
        with self.assertRaises(CatalogError) as caught:
            collect(basic(sheet_rels=traversal),{'table_name':'Devices'})
        self.assertEqual(caught.exception.code,'relationship_escapes_package')
        encoded='<Relationships xmlns="%s"><Relationship Id="t1_1" Type="%s/table" Target="..%%2f..%%2fsecret.xml"/></Relationships>'%(P,R)
        with self.assertRaises(CatalogError) as caught:
            collect(basic(sheet_rels=encoded),{'table_name':'Devices'})
        self.assertEqual(caught.exception.code,'relationship_target_invalid')

    def test_duplicate_zip_member_rejected(self):
        buffer=io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',UserWarning)
            with zipfile.ZipFile(buffer,'w') as archive:
                archive.writestr('a.xml','1');archive.writestr('a.xml','2')
        with self.assertRaises(CatalogError) as caught:
            collect_response(response(buffer.getvalue()),{'table_name':'Devices'})
        self.assertEqual(caught.exception.code,'archive_member_duplicate')

    def test_compression_ratio_and_budget_rejected(self):
        body=basic()
        with self.assertRaises(CatalogError) as caught:
            collect(body,{'table_name':'Devices'},{'max_rows':1})
        self.assertEqual(caught.exception.code,'rows_budget')
        with self.assertRaises(CatalogError) as caught:
            collect_response(response(body,media='text/html'),{'table_name':'Devices'})
        self.assertEqual(caught.exception.code,'media_type_unsupported')

    def test_external_links_registered_not_followed(self):
        rels=['<Relationship Id="el" Type="%s/externalLink" Target="file:///C:/links.xlsx" TargetMode="External"/>'%R,
              '<Relationship Id="vba" Type="%s/vbaProject" Target="vbaProject.bin"/>'%R]
        body=basic(workbook_rels=rels,extra={'xl/vbaProject.bin':b'macro'})
        result=collect(body,{'table_name':'Devices','field_mapping':{'编号':'id'}})
        self.assertEqual(result['workbook']['external_links'][0]['kind'],'externalLink')
        self.assertTrue(result['workbook']['external_links'][0]['external'])
        self.assertEqual(result['workbook']['calculation']['external_links_followed'],False)
        self.assertEqual(result['workbook']['calculation']['macros_executed'],False)
        self.assertEqual(result['workbook']['macro_part']['target'],'vbaProject.bin')

    def test_content_types_required_for_workbook(self):
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('xl/workbook.xml','<workbook/>')
        with self.assertRaises(CatalogError) as caught:
            collect_response(response(buffer.getvalue()),{'table_name':'Devices'})
        self.assertEqual(caught.exception.code,'package_content_types_missing')

    def test_record_sink_streams_without_accumulating(self):
        rows=BASE_ROWS+''.join(row(number,num('A%d'%number,str(number))) for number in range(2,12))
        body=basic(rows=rows,table=table_xml('Devices','A1:C11',['编号','日期','备注']))
        seen=[]
        result=collect_response(response(body),{'table_name':'Devices','field_mapping':{'编号':'id'}},
                                record_sink=seen.append)
        self.assertNotIn('rows',result)
        self.assertEqual(result['rows_streamed'],10)
        self.assertEqual([record['values']['id']['value'] for record in seen],list(range(2,12)))


if __name__=='__main__':unittest.main()
