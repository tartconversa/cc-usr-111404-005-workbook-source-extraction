"""有界读取 OPC 包、解析 XML 与关系；只沿已声明关系访问部件。

本模块在领取阶段拒绝：超预算、重复成员、可疑压缩比、加密成员、
包名越界、XML 实体/深度、关系目标越出包或被登记为外链。
读取结果按部件缓存，因此同一部件只解压一次并只计一次总预算。
"""
import io
import posixpath
import re
import zipfile
from dataclasses import asdict, dataclass
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from .errors import CatalogError

_DTD_RE=re.compile(rb'<!DOCTYPE|<!ENTITY',re.IGNORECASE)
_SCHEME_RE=re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')

@dataclass(frozen=True)
class Limits:
    """读取上界；调用方可用 as_dict 只覆盖部分字段。"""
    max_archive_bytes:int=32*1024*1024
    max_part_bytes:int=16*1024*1024
    max_total_uncompressed_bytes:int=64*1024*1024
    max_members:int=2048
    max_compression_ratio:int=200
    min_ratio_check_bytes:int=1024*1024
    max_xml_depth:int=256
    max_sheets:int=2048
    max_tables:int=4096
    max_rows:int=200000
    max_cells:int=2000000
    max_shared_strings:int=2000000

    def __post_init__(self):
        for key,value in asdict(self).items():
            if type(value) is not int or value<=0:
                raise ValueError('读取预算无效: '+key)

    def as_dict(self):return asdict(self)

    def replace(self,overrides=None):
        values=asdict(self)
        for key,value in (overrides or {}).items():
            if key not in values:raise ValueError('未知读取预算: '+key)
            values[key]=value
        return Limits(**values)

@dataclass(frozen=True)
class Relationship:
    id:str
    type:str
    target:str
    target_mode:str
    rels_part:str
    source_part:str

    @property
    def kind(self):return self.type.rstrip('/').rsplit('/',1)[-1]
    @property
    def external(self):return self.target_mode.lower()=='external'
    def location(self):return {'rels_part':self.rels_part,'relationship_id':self.id,'source_part':self.source_part,'target':self.target}

def local_name(tag):
    return tag.rsplit('}',1)[-1] if isinstance(tag,str) else tag

def attr(elem,name,default=None):
    """先取无命名空间同名属性，再取任意命名空间的同名属性。"""
    if name in elem.attrib:return elem.attrib[name]
    for key,value in elem.attrib.items():
        if local_name(key)==name:return value
    return default

def relationship_id(elem):
    for key,value in elem.attrib.items():
        if key.startswith('{') and local_name(key)=='id' and 'relationships' in key:
            return value
    return elem.attrib.get('r:id')

def rels_part_for(part):
    if part in ('','/'):return '/_rels/.rels'
    directory,name=posixpath.split(part)
    return directory+'/_rels/'+name+'.rels'

def _check_member_name(name):
    if not name:return '空成员名'
    if '\x00' in name:return '成员名含空字符'
    if '\\' in name:return '成员名含反斜杠'
    if name.startswith('/'):return '成员名为绝对路径'
    for segment in name.split('/'):
        if segment in ('','.','..'):return '成员名含越界或空路径段'
    return None

def _normalise_segments(segments):
    result=[]
    for segment in segments:
        if segment=='':continue
        if segment=='.':continue
        if segment=='..':
            if not result:return None
            result.pop();continue
        result.append(segment)
    return result

class Package:
    """只读、有界的 OPC 包视图。"""

    def __init__(self,body,limits):
        if not isinstance(body,(bytes,bytearray)):raise TypeError('包体必须是字节')
        limits=limits or Limits()
        self.limits=limits
        if len(body)>limits.max_archive_bytes:
            raise CatalogError('archive_too_large','响应实体超过读取预算',{'package':True},size=len(body),limit=limits.max_archive_bytes)
        self.archive_bytes=len(body)
        try:self._archive=zipfile.ZipFile(io.BytesIO(body))
        except (zipfile.BadZipFile,OSError,ValueError) as exc:
            raise CatalogError('archive_malformed','不是可读取的ZIP包',{'package':True}) from exc
        infos=self._archive.infolist()
        if len(infos)>limits.max_members:
            raise CatalogError('archive_member_limit','压缩包成员数超过预算',{'package':True},members=len(infos),limit=limits.max_members)
        self._names={}
        self._lower={}
        total=0
        for info in infos:
            if info.is_dir():continue
            problem=_check_member_name(info.filename)
            if problem:raise CatalogError('archive_member_invalid',problem,{'part':info.filename})
            if info.flag_bits & 0x1:raise CatalogError('archive_member_encrypted','加密成员不能在领取阶段解释',{'part':info.filename})
            if info.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):
                raise CatalogError('archive_compression_unsupported','不支持的压缩方法',{'part':info.filename},method=info.compress_type)
            part='/'+info.filename
            folded=part.casefold()
            if part in self._names or folded in self._lower:
                raise CatalogError('archive_member_duplicate','ZIP 成员重复（含大小写等价）',{'part':part},duplicate_of=self._names.get(part) or self._lower.get(folded))
            if info.file_size>limits.max_part_bytes:
                raise CatalogError('archive_part_too_large','部件解压大小超过预算',{'part':part},size=info.file_size,limit=limits.max_part_bytes)
            if info.file_size>=limits.min_ratio_check_bytes:
                if info.compress_size<=0 or info.file_size/info.compress_size>limits.max_compression_ratio:
                    raise CatalogError('archive_expansion_suspicious','部件压缩比异常',{'part':part},size=info.file_size,compressed=info.compress_size,ratio_limit=limits.max_compression_ratio)
            total+=info.file_size
            if total>limits.max_total_uncompressed_bytes:
                raise CatalogError('archive_expansion_budget','解压总大小超过预算',{'package':True},total=total,limit=limits.max_total_uncompressed_bytes)
            self._names[part]=info
            self._lower[folded]=part
        self._cache={}
        self._root_cache={}
        self._inflated=0

    def __enter__(self):return self
    def __exit__(self,*args):self._archive.close()

    def close(self):self._archive.close()

    @property
    def parts(self):return sorted(self._names)

    def exists(self,part):return part in self._names

    def read_part(self,part):
        if part in self._cache:return self._cache[part]
        info=self._names.get(part)
        if info is None:raise CatalogError('package_part_missing','关系指向的部件不存在',{'part':part})
        try:
            with self._archive.open(info,'r') as stream:
                chunks=[];size=0
                while True:
                    chunk=stream.read(1024*1024)
                    if not chunk:break
                    size+=len(chunk)
                    if size>self.limits.max_part_bytes:
                        raise CatalogError('archive_part_too_large','实际解压大小超过预算',{'part':part},size=size,limit=self.limits.max_part_bytes)
                    chunks.append(chunk)
        except CatalogError:raise
        except (RuntimeError,zipfile.BadZipFile,NotImplementedError,OSError) as exc:
            raise CatalogError('package_part_unreadable','部件解压失败',{'part':part}) from exc
        if size!=info.file_size:
            raise CatalogError('package_part_size_mismatch','声明大小与实际解压大小不一致',{'part':part},declared=info.file_size,actual=size)
        self._inflated+=size
        if self._inflated>self.limits.max_total_uncompressed_bytes:
            raise CatalogError('archive_expansion_budget','实际解压总大小超过预算',{'package':True},total=self._inflated,limit=self.limits.max_total_uncompressed_bytes)
        data=b''.join(chunks)
        self._cache[part]=data
        return data

    def iter_xml(self,part):
        """事件迭代；(start,元素) 保留子节点，(end,元素) 之后元素已被清空。"""
        data=self.read_part(part)
        if _DTD_RE.search(data):
            raise CatalogError('xml_entity_forbidden','XML 含 DTD 或实体声明',{'part':part})
        depth=0
        try:
            for event,elem in ET.iterparse(io.BytesIO(data),events=('start','end')):
                if event=='start':
                    depth+=1
                    if depth>self.limits.max_xml_depth:
                        raise CatalogError('xml_depth_exceeded','XML 嵌套超过预算',{'part':part},depth=depth,limit=self.limits.max_xml_depth)
                else:depth-=1
                yield event,elem
        except ET.ParseError as exc:
            raise CatalogError('xml_malformed','XML 解析失败',{'part':part},detail=str(exc)) from exc

    def xml_root(self,part):
        if part in self._root_cache:return self._root_cache[part]
        root=None
        for event,elem in self.iter_xml(part):
            if event=='start' and root is None:root=elem
        if root is None:raise CatalogError('xml_empty','XML 部件为空',{'part':part})
        self._root_cache[part]=root
        return root

    # 兼容简写：部分调用方按属性访问预算。

    def relationships(self,source_part):
        """读取 source_part 的 .rels；缺失时返回空关系。"""
        rels_part=rels_part_for(source_part)
        if not self.exists(rels_part):
            if source_part=='/':
                raise CatalogError('relationship_part_missing','包根关系缺失',{'part':rels_part})
            return rels_part,()
        root=self.xml_root(rels_part)
        if local_name(root.tag)!='Relationships':
            raise CatalogError('relationship_part_malformed','关系部件根元素不是 Relationships',{'part':rels_part})
        result=[];seen={}
        for elem in root:
            if local_name(elem.tag)!='Relationship':continue
            rel_id=attr(elem,'Id')
            rel_type=attr(elem,'Type')
            target=attr(elem,'Target')
            mode=attr(elem,'TargetMode') or 'Internal'
            location={'part':rels_part,'relationship_id':rel_id}
            if not rel_id or not rel_type or target is None:
                raise CatalogError('relationship_malformed','关系缺少 Id、Type 或 Target',location)
            if rel_id in seen:
                raise CatalogError('relationship_duplicate','同一关系部件内关系 Id 重复',location,first=seen[rel_id])
            seen[rel_id]=rel_id
            if mode.lower() not in ('internal','external'):
                raise CatalogError('relationship_mode_invalid','关系 TargetMode 非法',location,target_mode=mode)
            result.append(Relationship(rel_id,rel_type,target,mode,rels_part,source_part))
        return rels_part,tuple(result)

    def resolve(self,rel,*,allow_external=False):
        """把关系目标解析成包内部件名；任何越界都在这里拒绝。"""
        if rel.external:
            if allow_external:return None
            raise CatalogError('relationship_external','所需关系登记为 External，不领取包外目标',rel.location())
        if not rel.target or _SCHEME_RE.match(rel.target.strip()) or rel.target.strip().startswith('//'):
            raise CatalogError('relationship_target_invalid','关系目标不是包内相对或绝对路径',rel.location())
        if '%2f' in rel.target.lower() or '%5c' in rel.target.lower() or '%2e%2e' in rel.target.lower():
            raise CatalogError('relationship_target_invalid','关系目标用百分号编码分隔符或上级段，不接受',rel.location())
        if '%' in rel.target:
            try:raw=unquote(rel.target.strip(),errors='strict')
            except UnicodeDecodeError as exc:
                raise CatalogError('relationship_target_invalid','关系目标的百分号编码无效',rel.location()) from exc
        else:raw=rel.target.strip()
        if '\\' in raw or '\x00' in raw:
            raise CatalogError('relationship_target_invalid','关系目标含非法字符',rel.location())
        split=urlsplit(raw)
        if split.scheme or split.netloc or split.query or split.fragment:
            raise CatalogError('relationship_target_invalid','关系目标带协议、主机、查询或片段',rel.location())
        if raw.startswith('/'):
            segments=raw.split('/')[1:]
        else:
            base=posixpath.dirname(rel.source_part)
            segments=(base+'/'+raw).split('/')[1:] if base not in ('','/') else raw.split('/')
        if segments and segments[0]=='':segments=segments[1:]
        normalised=_normalise_segments(segments)
        if normalised is None:
            raise CatalogError('relationship_escapes_package','关系目标越出包根',rel.location())
        if not normalised:
            raise CatalogError('relationship_target_invalid','关系目标为空',rel.location())
        part='/'+'/'.join(normalised)
        if not self.exists(part):
            raise CatalogError('relationship_target_missing','关系目标不在 ZIP 成员中',rel.location(),resolved=part)
        return part

    def related(self,source_part,kind=None):
        rels_part,rels=self.relationships(source_part)
        if kind is None:return rels_part,rels
        return rels_part,tuple(rel for rel in rels if rel.kind==kind)

    def find_related(self,source_part,kind):
        rels_part,rels=self.related(source_part,kind)
        if not rels:return None
        if len(rels)>1:
            raise CatalogError('relationship_ambiguous','同一来源登记了多个目标类型相同的关系',{'part':source_part,'kind':kind},relationships=[rel.id for rel in rels])
        return rels[0]
