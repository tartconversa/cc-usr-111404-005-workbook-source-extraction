"""将来源字节身份与当前响应概览一起交给调用者。

名录采集入口在 app.catalog；这里保留原有的单响应概览。
"""
import json
import sys
from pathlib import Path
from .source import from_packet,fetch
from .baseline import analyze
from .catalog import collect_response

def inspect_response(response):
    return {'source_id':response.source_id,'response_sha256':response.sha256,'summary':analyze(response)}

def inspect(packet):return inspect_response(from_packet(packet))

def collect_one(url,source_id,**kwargs):return inspect_response(fetch(url,source_id,**kwargs))

def collect_catalog(packet,metadata=None,limits=None,record_sink=None):
    """从已领取的响应包读取名录；metadata 缺省时取 packet.metadata。"""
    return collect_response(from_packet(packet),metadata or packet.get('metadata'),limits,record_sink)

def main():
    if len(sys.argv)!=2:raise SystemExit('请指定来源响应JSON路径')
    print(json.dumps(inspect(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
