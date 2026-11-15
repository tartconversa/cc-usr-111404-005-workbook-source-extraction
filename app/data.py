"""A1 引用与行列坐标。"""
import re

from .errors import CatalogError

MAX_COLUMN=16384
MAX_ROW=1048576
_CELL_RE=re.compile(r'^\$?([A-Za-z]{1,3})\$?([0-9]{1,7})$')

def column_index(letters):
    index=0
    for char in letters.upper():
        index=index*26+(ord(char)-64)
    return index

def column_letters(index):
    if index<1:raise ValueError('列号必须从1开始')
    letters=''
    while index:
        index,remainder=divmod(index-1,26)
        letters=chr(65+remainder)+letters
    return letters

def cell_ref(row,column):
    return column_letters(column)+str(row)

def parse_cell_ref(text,location=None):
    match=_CELL_RE.match(text or '')
    if not match:raise CatalogError('cell_reference_invalid','单元格引用无效',location,ref=text)
    column=column_index(match.group(1));row=int(match.group(2))
    if column>MAX_COLUMN or row>MAX_ROW or row<1:
        raise CatalogError('cell_reference_out_of_bounds','单元格引用超出工作表上限',location,ref=text)
    return row,column

def parse_area(ref,location=None):
    """返回 (first_row,first_column,last_row,last_column)，按坐标而非书写顺序规范化。"""
    parts=(ref or '').split(':')
    if len(parts)>2 or not parts[0]:
        raise CatalogError('range_reference_invalid','区域引用无效',location,ref=ref)
    r1,c1=parse_cell_ref(parts[0],location)
    if len(parts)==1:return r1,c1,r1,c1
    r2,c2=parse_cell_ref(parts[1],location)
    return min(r1,r2),min(c1,c2),max(r1,r2),max(c1,c2)
