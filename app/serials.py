"""按日期系统解释 Excel 序列值；1900 年虚构闰日单独报告。

serial 60 在 1900 制中指向 1900-02-29，该日不存在；即使序列值词素
看起来是日期，解释值也保持未解释并给出 not_interpreted 原因。
1904 制下 serial 0 是 1904-01-01，不按 1900 制的偏移猜测。
"""
import datetime as _dt

_EPOCH_1900=_dt.date(1899,12,31)
_EPOCH_1904=_dt.date(1904,1,1)


def _format_date(day,fraction_micros):
    text=day.isoformat()
    if not fraction_micros:return text
    hours,remainder=divmod(fraction_micros,3600*10**6)
    minutes,remainder=divmod(remainder,60*10**6)
    seconds,micros=divmod(remainder,10**6)
    text+='T%02d:%02d:%02d'%(hours,minutes,seconds)
    if micros:text+='.%06d'%micros
    return text


def interpret_serial(value,date_system):
    """返回 {'value','kind','text','notes'}；未解释时 value 为 None。

    date_system 只能是 1900 或 1904，二者偏移不同，绝不互相替代。
    1900 制 serial 1 为 1900-01-01，serial 59 为 1900-02-28，serial 60
    是 Excel 为兼容 Lotus 1-2-3 虚构的 1900-02-29，永不映射；serial 61
    为 1900-03-01。1904 制 serial 0 为 1904-01-01。
    """
    if date_system not in (1900,1904):
        raise ValueError('日期制必须是 1900 或 1904')
    if value!=value or value in (float('inf'),float('-inf')):
        return {'value':None,'kind':None,'text':None,'notes':['序列值为非有限数，未解释']}
    whole=int(value//1)
    micros=round((value-whole)*86400*10**6)
    if micros>=86400*10**6:
        whole+=1;micros=0
    if date_system==1904:
        if whole<0:
            return {'value':None,'kind':None,'text':None,'notes':['序列值为负，无法表示为 1904 日期制日期，未解释']}
        day=_EPOCH_1904+_dt.timedelta(days=whole)
        return {'value':_format_date(day,micros),'kind':'date','text':_format_date(day,micros),'notes':[]}
    if whole<=0:
        return {'value':None,'kind':None,'text':None,'notes':['序列值小于等于 0，无法表示为 1900 日期制日期，未解释']}
    if whole==60:
        return {'value':None,'kind':None,'text':None,'notes':['serial 60 对应 1900 年虚构闰日 1900-02-29，未解释']}
    # 1900 制在 serial 60 处虚增一天，因此 61 起真实日期需回退一天。
    if whole<60:day=_EPOCH_1900+_dt.timedelta(days=whole)
    else:day=_EPOCH_1900+_dt.timedelta(days=whole-1)
    return {'value':_format_date(day,micros),'kind':'date','text':_format_date(day,micros),'notes':[]}

