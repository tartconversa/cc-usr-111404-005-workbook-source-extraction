"""识别数字格式类别：General、日期时间、数值与带前导零的编号占位格式。

只解释已知族的格式；未知格式保留原词素并标注未解释，绝不猜测显示
结果。识别按 Excel 格式串规则处理引号字面量、反斜杠转义、填充/对齐
与方括号（颜色、条件、区域/货币），只有经过时间 [h]/[mm]/[ss] 才当作
日期时间记号。
"""
import re

_BUILTIN_GENERAL=0
_BUILTIN_TEXT=49
_BUILTIN_DATE_IDS={14,15,16,17,18,19,20,21,22,45,46,47,50,51,52,53,54,55,56,57,58}
_BUILTIN_NUMBER_IDS=set(range(1,50))-{_BUILTIN_TEXT}
_ZERO_ONLY_RE=re.compile(r'^0+$')
_NUMERIC_RESIDUE_RE=re.compile(r'^[0#?.,%Ee+\-\s()]+$')
_ELAPSED_RE=re.compile(r'^\[(h+|m+|s+)\]$',re.IGNORECASE)


def _split_sections(code):
    """按未加引号、未转义的分号切分正数;负数;零;文本四段。"""
    sections=[];current=[];quoted=False;i=0
    while i<len(code):
        char=code[i]
        if char=='"':
            quoted=not quoted;current.append(char)
        elif char=='\\' and not quoted and i+1<len(code):
            current.append(char);current.append(code[i+1]);i+=1
        elif char in ('_','*') and not quoted and i+1<len(code):
            current.append(char);current.append(code[i+1]);i+=1
        elif char==';' and not quoted:
            sections.append(''.join(current));current=[]
        else:current.append(char)
        i+=1
    sections.append(''.join(current))
    return sections


def _strip_display_tokens(section):
    """去掉字面量与显示修饰，返回真正的格式占位序列。"""
    out=[];i=0
    while i<len(section):
        char=section[i]
        if char=='"':
            i+=1
            while i<len(section) and section[i]!='"':i+=1
            i+=1;continue
        if char in ('\\','_','*'):
            i+=2;continue
        if char=='[':
            end=section.find(']',i)
            if end==-1:
                out.append(section[i:]);break
            content=section[i+1:end]
            if _ELAPSED_RE.match(section[i:end+1]):
                out.append(section[i:end+1])
            # 其余方括号是颜色、条件、区域或货币，不属于格式占位。
            i=end+1;continue
        out.append(char);i+=1
    return ''.join(out)


def _is_date_time(text):
    lowered=text.lower()
    if lowered in ('yyyy-mm-dd','hh:mm','hh:mm:ss'):return True
    has_year='yy' in lowered
    has_day='d' in lowered
    has_hour='h' in lowered
    has_second='s' in lowered
    has_month='m' in lowered
    if has_hour and (has_second or has_month or has_day):return True
    if has_second and has_month:return True
    if has_year and (has_month or has_day):return True
    if has_day and has_month and not has_year:return True
    if re.search(r'(m{3,}|d{1,4}[/\-.]m{3,})',lowered):return True
    return False


def classify_format(code,builtin_id=None):
    """返回 {'classification','format_code','format_id'}。

    classification 取 general、text、date_time、zero_padded_code、number 或
    unknown。builtin_id 已知时优先采用其内建语义；未知内建 id 仍按格式串
    判断，避免把来源的样式差异当成未知。
    """
    if builtin_id in _BUILTIN_DATE_IDS:
        return {'classification':'date_time','format_code':code,'format_id':builtin_id}
    if builtin_id==_BUILTIN_GENERAL:
        return {'classification':'general','format_code':code or 'General','format_id':builtin_id}
    if builtin_id==_BUILTIN_TEXT:
        return {'classification':'text','format_code':code or '@','format_id':builtin_id}
    if code is None or code=='' or code.strip().lower()=='general':
        return {'classification':'general','format_code':'General','format_id':builtin_id}
    saw_number=False
    for section in _split_sections(code):
        cleaned=_strip_display_tokens(section).strip()
        if not cleaned:continue
        if cleaned=='@':
            return {'classification':'text','format_code':code,'format_id':builtin_id}
        if _ZERO_ONLY_RE.match(cleaned):
            return {'classification':'zero_padded_code','format_code':code,'format_id':builtin_id,
                    'zeros':len(cleaned)}
        if _is_date_time(cleaned):
            return {'classification':'date_time','format_code':code,'format_id':builtin_id}
        if _NUMERIC_RESIDUE_RE.match(cleaned) and re.search(r'[0#?]',cleaned):
            saw_number=True
    if saw_number or builtin_id in _BUILTIN_NUMBER_IDS:
        return {'classification':'number','format_code':code,'format_id':builtin_id}
    return {'classification':'unknown','format_code':code,'format_id':builtin_id}
