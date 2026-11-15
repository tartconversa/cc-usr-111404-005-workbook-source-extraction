"""解释单个工作表单元格：类型、原始词素、格式与解释状态。

原始值只按词素保留，绝不从 Python 类型反推单元格类型。以下状态彼此
独立，不合并：

    text            文本单元格的非空字符串
    empty_string    文本单元格的空字符串（<t></t> 或 <v></v>，t="s"/"str"）
    empty_value     单元格存在但没有值（数字 <c> 无 <v>，或内联字符串无 <t>）
    missing_cell    该坐标没有单元格记录（稀疏表）
    error           错误值单元格（t="e"，如 #N/A、#DIV/0!）
    number / date / boolean
    formula_cache_present / formula_cache_missing
    not_interpreted 词素无法按声明类型或格式解释

公式只读取缓存：cache_present 表示文件里有无缓存，cache_usable 表示
缓存词素是否被解释成可用的值。不执行公式，不调用计算引擎，不把
calcPr 当作缓存新鲜。
"""
import math
from datetime import date, datetime

from .errors import CatalogError
from .formats import classify_format
from .ooxml import attr, local_name
from .serials import interpret_serial

_STATUS_BY_STATE={
    'text':'text','empty_string':'text',
    'number':'number','date':'date','boolean':'boolean',
    'error':'error','empty_value':'empty','missing_cell':'empty',
    'formula_cache_present':'formula','formula_cache_missing':'formula',
    'not_interpreted':'not_interpreted',
}

_CELL_TYPES={'n','s','str','inlineStr','b','e','d'}
_ERROR_CODES={'#NULL!','#DIV/0!','#VALUE!','#REF!','#NAME?','#NUM!','#N/A',
              '#GETTING_DATA','#SPILL!','#CALC!','#FIELD!','#BLOCKED!','#UNKNOWN!','#CONNECT!'}


def status_for(state):
    return _STATUS_BY_STATE.get(state,'not_interpreted')


def _parse_number(text):
    stripped=(text or '').strip()
    if not stripped:return None
    try:return int(stripped)
    except ValueError:pass
    try:parsed=float(stripped)
    except ValueError:return None
    if not math.isfinite(parsed):return None
    return parsed


def _date_from_iso(text):
    """只按 ISO-8601 形态解释 t="d"，不做本地化或序列值猜测。"""
    candidate=(text or '').strip()
    if not candidate:return None
    if candidate.endswith('Z'):candidate=candidate[:-1]+'+00:00'
    for parser in (datetime.fromisoformat,date.fromisoformat):
        try:parsed=parser(candidate)
        except ValueError:continue
        return parsed.isoformat()
    return None


def _number_text(value):
    if type(value) is int:return str(value)
    if value==int(value):return str(int(value))
    return repr(value)


def _record(state,text=None,raw=None,interpretation=None,fmt=None,notes=None,**extra):
    result={'state':state,'status':status_for(state),'text':text,'raw':raw,
            'interpretation':interpretation,'notes':list(notes or [])}
    if fmt is not None:result['format']=fmt
    result.update(extra)
    return result


def describe_cell(cell_elem,location):
    """把 <c> 拆成不解码的原始结构；未知元素与类型在这里拒绝。"""
    cell_type=attr(cell_elem,'t')
    if cell_type is not None and cell_type not in _CELL_TYPES:
        raise CatalogError('cell_type_invalid','未识别的单元格类型',location,type=cell_type)
    style=attr(cell_elem,'s')
    style_index=0
    if style is not None:
        if not str(style).isdigit():
            raise CatalogError('cell_style_invalid','单元格样式索引不是非负整数',location,style=style)
        style_index=int(style)
    description={'type':cell_type or 'n','style':style_index,'value_text':None,
                 'formula':None,'formula_meta':None,'cache_present':False,
                 'inline_text':None,'inline_has_text':False,'has_formula':False}
    seen=set()
    for child in cell_elem:
        name=local_name(child.tag)
        if name in ('f','v','is'):
            if name in seen:
                raise CatalogError('cell_value_malformed','单元格含重复的子元素',location,element=name)
            seen.add(name)
        if name=='f':
            description['has_formula']=True
            description['formula']=child.text or None
            description['formula_meta']=_formula_meta(child)
        elif name=='v':
            description['value_text']=child.text
            description['cache_present']=True
        elif name=='is':
            text,has_text=_rich_text(child,location)
            description['inline_text']=text
            description['inline_has_text']=has_text
        elif name=='extLst':
            continue
        else:
            raise CatalogError('cell_value_malformed','单元格含未知子元素',location,element=name)
    return description


def _formula_meta(formula_elem):
    meta={}
    for key,value in formula_elem.attrib.items():
        local=key.rsplit('}',1)[-1]
        if local in ('t','ref','si','aca','bx'):meta[local]=value
    return meta or None


def _rich_text(container,location):
    pieces=[];has_text=False
    for child in container:
        name=local_name(child.tag)
        if name=='t':pieces.append(child.text or '');has_text=True
        elif name=='r':
            for grandchild in child:
                if local_name(grandchild.tag)=='t':
                    pieces.append(grandchild.text or '');has_text=True
                elif local_name(grandchild.tag)!='rPr':
                    raise CatalogError('cell_value_malformed','内联字符串富文本 run 含未知子元素',
                                       location,element=local_name(grandchild.tag))
        elif name in ('rPh','phoneticPr','extLst'):continue
        else:
            raise CatalogError('cell_value_malformed','内联字符串含未知子元素',location,element=name)
    return ''.join(pieces),has_text


class CellDecoder:
    """绑定一份工作簿的共享字符串、样式与日期制，解释单元格。"""

    def __init__(self,shared_strings,style_map,num_dates):
        self.shared_strings=shared_strings
        self.style_map=style_map
        self.num_dates=num_dates
        self._formats={}

    def format_for(self,style_index):
        if style_index is None:return classify_format(None,None)
        cached=self._formats.get(style_index)
        if cached is not None:return cached
        if style_index not in self.style_map:
            if style_index==0 and not self.style_map:result=classify_format(None,None)
            else:
                raise CatalogError('cell_style_out_of_range','单元格样式索引超出 cellXfs，日期与数字格式无法解释',
                                   {'style_index':style_index,'style_count':len(self.style_map)})
        else:result=classify_format(*self.style_map[style_index])
        self._formats[style_index]=result
        return result

    def decode(self,described,location):
        """described 为 None 表示该坐标没有单元格记录。"""
        if described is None:
            return _record('missing_cell',notes=['该坐标没有单元格记录（缺单元格）'])
        cell_type=described.get('type') or 'n'
        fmt=self.format_for(described.get('style'))
        notes=[]
        if described.get('has_formula'):
            return self._decode_formula(described,cell_type,fmt,notes)
        if cell_type=='s':return self._decode_shared_string(described,fmt)
        if cell_type=='inlineStr':return self._decode_inline(described,fmt)
        if cell_type=='str':return self._decode_str_cache(described,fmt)
        if cell_type=='b':return self._decode_boolean(described,fmt)
        if cell_type=='e':return self._decode_error(described,fmt)
        if cell_type=='d':return self._decode_iso_date(described,fmt)
        return self._decode_number(described.get('value_text'),fmt,notes)

    def _decode_shared_string(self,cell,fmt):
        index_text=cell.get('value_text')
        if index_text is None or not index_text.strip().isdigit():
            return _record('not_interpreted',raw=index_text,fmt=fmt,
                           notes=['共享字符串索引无效，未解释'])
        index=int(index_text.strip())
        if index>=len(self.shared_strings):
            return _record('not_interpreted',raw=index_text,fmt=fmt,shared_index=index,
                           notes=['共享字符串索引越界，未解释'])
        text=self.shared_strings[index]
        return _record('text' if text!='' else 'empty_string',text=text,fmt=fmt,shared_index=index,
                       notes=[] if text!='' else ['空字符串（共享字符串）'])

    def _decode_inline(self,cell,fmt):
        if not cell.get('inline_has_text'):
            return _record('empty_value',fmt=fmt,notes=['内联字符串没有 <t>，按无值单元格处理'])
        text=cell.get('inline_text') or ''
        return _record('text' if text!='' else 'empty_string',text=text,fmt=fmt,
                       notes=[] if text!='' else ['空字符串（内联字符串）'])

    def _decode_str_cache(self,cell,fmt):
        text=cell.get('value_text')
        if text is None:return _record('empty_value',fmt=fmt,notes=['字符串单元格没有 <v>'])
        return _record('text' if text!='' else 'empty_string',text=text,raw=text,fmt=fmt,
                       notes=[] if text!='' else ['空字符串（t="str"）'])

    def _decode_boolean(self,cell,fmt):
        raw=cell.get('value_text')
        folded=(raw or '').strip().lower()
        if folded not in ('0','1','true','false'):
            return _record('not_interpreted',raw=raw,fmt=fmt,notes=['布尔词素无效，未解释'])
        return _record('boolean',text=raw,raw=raw,interpretation=folded in ('1','true'),fmt=fmt)

    def _decode_error(self,cell,fmt):
        raw=cell.get('value_text')
        notes=[]
        if raw and raw.strip() not in _ERROR_CODES:notes.append('错误码不在已知集合内，仍按来源错误值保留')
        return _record('error',text=raw,raw=raw,fmt=fmt,notes=notes)

    def _decode_iso_date(self,cell,fmt):
        raw=cell.get('value_text')
        iso=_date_from_iso(raw)
        if iso is None:
            return _record('not_interpreted',raw=raw,fmt=fmt,notes=['日期字符串不是 ISO-8601，未解释'])
        return _record('date',text=raw,raw=raw,interpretation=iso,fmt=fmt)

    def _decode_number(self,value_text,fmt,notes):
        if value_text is None:
            return _record('empty_value',fmt=fmt,notes=['数字单元格没有 <v>，按无值单元格处理'])
        number=_parse_number(value_text)
        if number is None:
            return _record('not_interpreted',raw=value_text,fmt=fmt,notes=['数字词素无法解析，未解释'])
        if fmt['classification']=='date_time':
            serial=interpret_serial(number,self.num_dates)
            notes=list(notes)+serial['notes']
            if serial['value'] is None:
                return _record('not_interpreted',text=value_text,raw=value_text,fmt=fmt,
                               notes=notes,number=number,serial=number)
            return _record('date',text=value_text,raw=value_text,interpretation=serial['value'],
                           fmt=fmt,notes=notes,number=number,serial=number)
        return _record('number',text=value_text,raw=value_text,interpretation=number,fmt=fmt,
                       notes=notes,number=number,display=_display_for(number,fmt,notes))

    def _decode_formula(self,cell,cell_type,fmt,notes):
        meta=cell.get('formula_meta')
        formula={'expression':cell.get('formula'),'cache_present':bool(cell.get('cache_present')),
                 'cache_usable':False}
        if meta:formula['meta']=meta
        if cell_type=='inlineStr':
            formula['cache_present']=bool(cell.get('inline_has_text'))
        if not formula['cache_present']:
            if not formula['expression']:
                notes.append('公式单元格没有表达式（可能引用共享公式），也没有缓存值')
            return _record('formula_cache_missing',fmt=fmt,formula=formula,
                           notes=['公式单元格没有缓存值；未重算，值不可用']+notes)
        if not formula['expression']:notes.append('公式表达式为空（可能引用共享公式），只保留缓存')
        if cell_type=='e':
            record=_record('formula_cache_present',text=cell.get('value_text'),raw=cell.get('value_text'),
                           fmt=fmt,formula=formula,notes=['公式缓存值是错误值，保留但不作为可用值'])
            record['formula']['cache_error']=cell.get('value_text')
            return record
        if cell_type=='s':
            record=self._decode_shared_string(cell,fmt)
            if record['state'] not in ('text','empty_string'):
                notes.append('公式的共享字符串缓存无法解释')
                return _record('formula_cache_present',fmt=fmt,formula=formula,notes=notes)
            value=record.get('text');kind='string'
        elif cell_type=='inlineStr':
            if not cell.get('inline_has_text'):
                return _record('formula_cache_missing',fmt=fmt,formula=formula,
                               notes=['公式单元格没有可用的内联字符串缓存'])
            value=cell.get('inline_text') or '';kind='string'
        elif cell_type=='str':
            value=cell.get('value_text');kind='string'
        elif cell_type=='b':
            folded=(cell.get('value_text') or '').strip().lower()
            if folded not in ('0','1','true','false'):
                notes.append('公式布尔缓存词素无效，未解释')
                return _record('formula_cache_present',fmt=fmt,formula=formula,notes=notes,
                               raw=cell.get('value_text'))
            value=folded in ('1','true');kind='boolean'
        elif cell_type=='d':
            value=_date_from_iso(cell.get('value_text'))
            if value is None:
                notes.append('公式日期缓存不是 ISO-8601，未解释')
                return _record('formula_cache_present',fmt=fmt,formula=formula,notes=notes,
                               raw=cell.get('value_text'))
            kind='date'
        else:
            number=_parse_number(cell.get('value_text'))
            if number is None:
                notes.append('公式数字缓存词素无法解析，未解释')
                return _record('formula_cache_present',fmt=fmt,formula=formula,notes=notes,
                               raw=cell.get('value_text'))
            formula['cache_usable']=True
            if fmt['classification']=='date_time':
                serial=interpret_serial(number,self.num_dates)
                notes.extend(serial['notes'])
                if serial['value'] is None:
                    formula['cache_usable']=False
                    return _record('formula_cache_present',text=cell.get('value_text'),
                                   raw=cell.get('value_text'),fmt=fmt,formula=formula,notes=notes,
                                   number=number,serial=number)
                return _record('formula_cache_present',text=cell.get('value_text'),
                               raw=cell.get('value_text'),interpretation=serial['value'],fmt=fmt,
                               formula=formula,notes=notes,number=number,serial=number)
            return _record('formula_cache_present',text=cell.get('value_text'),
                           raw=cell.get('value_text'),interpretation=number,fmt=fmt,formula=formula,
                           notes=notes,number=number,display=_display_for(number,fmt,notes))
        formula['cache_usable']=True
        return _record('formula_cache_present',text=value if kind=='string' else cell.get('value_text'),
                       raw=cell.get('value_text'),interpretation=value,fmt=fmt,formula=formula,
                       notes=notes)


def _display_for(number,fmt,notes):
    classification=fmt['classification']
    if classification=='zero_padded_code':
        if type(number) is float and number!=int(number):
            notes.append('编号格式作用于非整数，未生成显示文本')
            return None
        sign='-' if number<0 else ''
        zeros=fmt.get('zeros') or 0
        return sign+str(abs(int(number))).zfill(zeros)
    if classification in ('general','number'):return _number_text(number)
    if classification=='unknown':
        notes.append('数字格式未识别，只保留原值，不推断显示文本')
        return None
    return None
