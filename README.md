# 供应商名录工作簿的关系定位与类型保真采集

现有组件读取一份来源响应，输出当前概览及字节摘要；新增的名录采集入口沿工作簿关系定位表对象、按列身份映射字段并保真记录单元格，尚不具备持续领取或跨响应业务归并能力。
app/source.py保留响应状态、重复头和实体原字节，一次请求不自动跟随重定向；app/baseline.py解释当前单响应，app/service.py将概览绑定到来源身份及摘要。
app/lab.py提供回环HTTP来源，支持GET、POST、HEAD、PROPFIND和REPORT，记录收到的请求，用于在可控来源上联调。fixtures为合成资料，不连接真实企业或个人。
来源协议、字段身份、名录结构与失败位置见[来源契约](docs/source-contract.md)。Python 3.14，现有代码仅使用标准库。

读取一份工作簿的名录（表对象按name定位，字段按tableColumn.name映射）：

读取当前响应概览：

    python -m app.service fixtures/sample.json

读取名录的程序入口（metadata来自来源配置）：

    from app.source import from_packet
    from app.catalog import collect_response, inspect_layout, relocalise
    packet = json.loads(...)
    result = collect_response(from_packet(packet), packet['metadata'])

inspect_layout 只读部件图与表对象；relocalise(previous, current, table_name) 在
工作簿换版后按表名与列身份重新定位，并报告列被移除或新增。

运行现有模块检查：

    python -m unittest discover -s tests -v

检查源码编译：

    python -m compileall -q app tests

如需单独启动本地来源，运行下列命令后读取打印的回环地址；结束时按Ctrl+C：

    python -m app.lab fixtures/sample.json
