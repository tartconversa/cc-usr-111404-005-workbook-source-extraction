"""工作表流式扫描：只物化目标区域内的行，不保留整张表。

OOXML 的 tableParts 通常位于 sheetData 之后，因此定位表对象需要先扫
一遍工作表拿到 tablePart 关系，再按表范围扫第二遍读取行。两遍都使用
增量 XML 解析：扫描时维护父元素栈，元素结束后立即 clear 并移出父节点，
内存只随嵌套深度和当前行增长，不随工作表总行数增长。表范围外的行
完全不再物化，只按列号推导跳过。
"""
from .data import parse_cell_ref
from .errors import CatalogError
from .ooxml import attr, local_name

MAX_ROWS=1048576
MAX_COLUMNS=16384


def _walk(package,part):
    """产出 (event, elem, parent, depth)。

    行结束后才清理该行及其子树：行内单元格在行结束前必须保持完整；
    清理后整行不再被工作表根节点引用，内存不随已处理行数增长。
    """
    stack=[]
    for event,elem in package.iter_xml(part):
        if event=='start':
            stack.append(elem)
            yield event,elem,stack[-2] if len(stack)>1 else None,len(stack)
        else:
            depth=len(stack)
            parent=stack[-2] if depth>1 else None
            yield event,elem,parent,depth
            stack.pop()
            # 单元格在行结束后才不再需要；整行从工作表根摘除并清空，
            # 使已处理数据不再被引用。其余元素保留，避免破坏仍在读取的父节点。
            if local_name(elem.tag)=='row' and parent is not None:
                parent.remove(elem)
                elem.clear()
            elif depth==2 and parent is not None:
                parent.remove(elem)
                elem.clear()


def scan_table_parts(package,part,location):
    """返回 (count, [关系Id])。没有 tableParts 时返回 (None, [])。"""
    count=None;ids=[];saw_parts=False;parts_depth=None
    for event,elem,parent,depth in _walk(package,part):
        name=local_name(elem.tag)
        if event=='start':
            if name=='tableParts':
                if depth!=2:
                    raise CatalogError('table_parts_malformed','tableParts 不是 worksheet 的直接子元素',location)
                if saw_parts:
                    raise CatalogError('table_parts_malformed','工作表含多个 tableParts',location)
                saw_parts=True;parts_depth=depth
                declared=attr(elem,'count')
                if declared is not None:
                    if not str(declared).isdigit():
                        raise CatalogError('table_parts_malformed','tableParts count 不是非负整数',location,
                                           declared=declared)
                    count=int(declared)
            elif parts_depth is not None and depth==parts_depth+1:
                if name!='tablePart':
                    raise CatalogError('table_parts_malformed','tableParts 含未知子元素',location,element=name)
                rel_id=_rel_id(elem)
                if not rel_id:
                    raise CatalogError('table_relationship_missing','tablePart 缺少关系 Id',location)
                ids.append(rel_id)
        else:
            if name=='tableParts':parts_depth=None
    if count is not None and count!=len(ids):
        raise CatalogError('table_parts_malformed','tableParts count 与 tablePart 数量不一致',
                           location,declared=count,actual=len(ids))
    return (count,ids) if saw_parts else (None,[])


def _rel_id(elem):
    for key,value in elem.attrib.items():
        if key.rsplit('}',1)[-1]=='id':return value
    return None


class RowScan:
    """逐行产出表范围内的行；只保留当前行。"""

    def __init__(self,package,part,location,bounds):
        self.package=package;self.part=part;self.location=location
        self.r1,self.c1,self.r2,self.c2=bounds
        self.outside_cells=0;self.cells_seen=0;self.rows_seen=[];self.saw_sheet_data=False
        self.duplicate_rows=[];self._seen=set()

    def run(self):
        previous_row=None
        sheet_data_depth=None
        sheet_data_elem=None
        saw_root=False
        for event,elem,parent,depth in _walk(self.package,self.part):
            name=local_name(elem.tag)
            if event=='start':
                if not saw_root:
                    saw_root=True
                    if name!='worksheet':
                        raise CatalogError('worksheet_malformed','工作表根元素不是 worksheet',self.location)
                elif name=='sheetData':
                    if depth!=2:
                        raise CatalogError('sheet_data_malformed','sheetData 不是 worksheet 的直接子元素',self.location)
                    if sheet_data_depth is not None:
                        raise CatalogError('sheet_data_duplicate','工作表含多个 sheetData',self.location)
                    sheet_data_depth=depth;sheet_data_elem=elem;self.saw_sheet_data=True
                continue
            if name=='sheetData':sheet_data_depth=None;sheet_data_elem=None
            if sheet_data_depth is None or name!='row' or parent is not sheet_data_elem:continue
            declared=attr(elem,'r')
            if declared is not None:
                if not str(declared).isdigit():
                    raise CatalogError('row_malformed','row/@r 不是正整数',self.location,value=declared)
                row_number=int(declared)
            else:
                row_number=1 if previous_row is None else previous_row+1
            if row_number<1 or row_number>MAX_ROWS:
                raise CatalogError('row_out_of_bounds','行号超出工作表上限',{**self.location,'row':row_number})
            previous_row=row_number
            if not (self.r1<=row_number<=self.r2):
                continue
            if row_number in self._seen:
                raise CatalogError('row_duplicate','表范围内同一行号出现多次',
                                   {**self.location,'row':row_number},table_ref=None)
            row=self._extract(elem,row_number)
            self._seen.add(row_number)
            self.rows_seen.append(row_number)
            if len(self.rows_seen)>self.package.limits.max_rows:
                raise CatalogError('rows_budget','表范围行数超过预算',{**self.location,'row':row_number},
                                   limit=self.package.limits.max_rows)
            yield row
        if not self.saw_sheet_data:
            raise CatalogError('sheet_data_missing','工作表没有 sheetData，不能把缺失当作空表',self.location)

    def _extract(self,row_elem,row_number):
        location={**self.location,'row':row_number}
        hidden=self._flag(attr(row_elem,'hidden'),'行隐藏属性',location)
        cells={};last_column=None
        for cell_elem in row_elem:
            if local_name(cell_elem.tag)=='extLst':continue
            if local_name(cell_elem.tag)!='c':
                raise CatalogError('row_malformed','row 含未知子元素',location,element=local_name(cell_elem.tag))
            reference=attr(cell_elem,'r')
            if reference is not None:
                row_from_ref,column=parse_cell_ref(reference,{**location,'ref':reference})
                if row_from_ref!=row_number:
                    raise CatalogError('cell_reference_conflict','单元格引用行号与所在行不符',
                                       {**location,'ref':reference})
            else:
                column=(last_column or 0)+1
            last_column=column
            if column<1 or column>MAX_COLUMNS:
                raise CatalogError('cell_reference_out_of_bounds','列号超出工作表上限',
                                   {**location,'column':column})
            if not (self.c1<=column<=self.c2):
                self.outside_cells+=1;continue
            self.cells_seen+=1
            if self.cells_seen>self.package.limits.max_cells:
                raise CatalogError('cells_budget','表范围内单元格数量超过预算',location,
                                   limit=self.package.limits.max_cells)
            if column in cells:
                raise CatalogError('cell_duplicate','同一坐标出现多个单元格',
                                   {**location,'column':column})
            from .cells import describe_cell
            cells[column]=describe_cell(cell_elem,{**location,'column':column})
        return {'row_number':row_number,'hidden':hidden,'cells':cells}

    def _flag(self,value,what,location):
        if value is None:return False
        folded=value.strip().lower()
        if folded in ('1','true','on'):return True
        if folded in ('0','false','off'):return False
        raise CatalogError('invalid_boolean',what+'不是合法布尔值',location,value=value)
