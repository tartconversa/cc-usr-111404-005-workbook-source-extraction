"""当前单响应概览。"""
import io,zipfile
def analyze(response):
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        names=archive.namelist()
        if len(set(names))!=len(names):raise ValueError('工作簿部件重复')
        return {'parts':sorted(names),'archive_bytes':len(response.body)}
