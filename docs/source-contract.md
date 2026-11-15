# 来源和当前接口

## 原始响应

fixtures/sample.json含source_id、url、status、headers、body_base64和可选metadata，headers为有序二元列表，保留重复字段；body_base64解码成真实原始实体字节，原件也存在fixtures/source-body.bin。
app.source.from_packet校验状态、头与字节编码，生成只读Response。header(name)不区分字段名大小写并按顺序逗号连接重复值，headers原元组始终保留。
sha256只证明当前实体字节，不证明来源业务身份或采集完整。fetch执行一次HTTP请求，保留错误响应，不自动跳转，不替来源解释缓存、分页或业务错误。
fetch(url,source_id,method,headers,body,max_bytes)保留方法和请求体，读取超过预算即失败；适配器现有超时为5秒。app.service.collect_one调用该来源接口，inspect(packet)供已有响应读取，二者输出source_id、response_sha256和summary。
实际访问资格由上层来源配置控制，本地来源服务只绑定127.0.0.1；其/source路径返回指定响应，其他路径404。服务器记录请求方法、路径、头与body_base64；现有记录只用于合成来源联调，不包含真实凭据。

## 业务来源口径

来源为.xlsx，不含加密或xls二进制。metadata.table_name指定Excel table的name，字段映射按tableColumn.name，重复或缺失列报告。数字原词素与解释值分别存，文本编号不数值化；数字格式需覆盖General、0占位编号和标准日期时间格式，未知格式保留原值及未解释状态。支持1900/1904日期制，serial60单列不可映射的虚构日期。公式保留表达式、缓存词素和可用状态，calcPr提示不等于缓存已新鲜。所有部件大小与总解压预算可配置，关系仅允许包内合法目标。

## 名录采集入口

app.catalog.collect_response(response, metadata, limits=None, record_sink=None)在领取阶段完成定位与保真读取，返回可复查结构；传record_sink时逐条回调、不累积rows，返回值给出rows_streamed。app.catalog.inspect_layout(response)只读部件图与表对象，app.catalog.relocalise(previous, current, table_name)比较两次布局（换版后重新定位），不比较行偏移也不合并记录。

    metadata={"table_name": "Devices",
              "field_mapping": {"设备编号": "device_id"},
              "hidden_sheets": "include", "hidden_rows": "include",
              "include_unmapped_columns": False,
              "limits": {"max_part_bytes": 16777216}}

输出字段：source（source_id、url、status、response_sha256）、workbook（media_type、archive_bytes、archive_sha256、workbook_part、date_system、shared_strings_part、styles_part、external_links、macro_part、calculation；media_type保留来源声明原值，内容类型按大小写不敏感比对）、table（name、display_name、sheet_name、sheet_state、sheet_order、sheet_visible、worksheet_part、table_part、ref、bounds、header_row_count、totals_row_count）、columns（表列名、table_column_id、index、sheet_column(_letters)、field、population、not_collected原因）、field_status、missing_configured_columns、rows、row_stats、config。

### 定位与失败位置

定位链固定为[Content_Types]——包根officeDocument关系——workbook——每张工作表的关系——tableParts关系——table。表名只按table/@name匹配；同名多表、同部件被多表引用、列名重复、范围与tableColumns数量不符、表头单元格与tableColumn/@name不一致都抛CatalogError。配置文件在读取包之前失败。失败位置location至少含part，相关时含rels_part、relationship_id、sheet_name、sheet_part、table_part、row、cell。

### 关系与包安全

采集器只沿关系访问部件：TargetMode="External"的关系在需要时直接拒绝；目标带协议、主机、反斜杠、百分号编码的分隔符或上级段、解析后越出包根、或不在ZIP成员中都拒绝。ZIP成员重复（含大小写等价）、加密成员、非Stored/Deflated压缩、可疑压缩比、部件声明大小或实际解压大小超预算、总解压超预算、DTD/实体声明、XML嵌套超限都拒绝；[Content_Types]与各部件内容类型逐级核对，不按扩展名猜内容。

### 列身份与字段

字段来自tableColumn/@name到field_mapping的映射，不依赖列偏移或工作表顺序；表头行只用于核对列身份，不一致即失败。未映射的列保留not_collected=not_mapped并给出原因，include_unmapped_columns为真时原样放入每条记录的unmapped；field_mapping登记但表列中不存在的字段进入missing_configured_columns与field_status（collected=false），不静默丢弃。hidden_sheets和hidden_rows只能取include或exclude，默认include并在输出标注；筛选/手工隐藏的行在文件中同为hidden="1"，无法从文件区分，都不当作删除。missing_rows只报告表范围内完全没有行记录的坐标，不补空行、不移位。

### 单元格解释

每个被映射字段输出state、status、raw、text、value、number/display、format（classification、format_code、format_id、interpreted、zeros）、notes。状态互不合并：missing_cell（无单元格记录）、empty_value（有单元格无值）、empty_string（文本空串）、text、number、date、boolean、error（t="e"）、formula_cache_present、formula_cache_missing、not_interpreted。前导零只在number+零占位格式时由zeros生成display，文本编号始终保持文本词素，不数值化。日期按workbookPr/@date1904选择1900或1904日期制；1900制serial 60的虚构闰日1900-02-29不映射，serial<=0与1904制负值同样标not_interpreted。公式只读<v>缓存（或内联字符串缓存），formula.cache_present与cache_usable分开报告；不执行公式、不执行宏、不跟随外链、不重算，calcPr只作提示。vbaProject只登记存在性与目标，不读入内容；externalLink只登记，不解析目标。

### 有界读取

解析用ElementTree增量事件，工作表先扫tableParts再按表范围扫行；表范围外的行与单元格不物化，只计数。预算：max_archive_bytes、max_part_bytes、max_total_uncompressed_bytes、max_members、max_compression_ratio、min_ratio_check_bytes、max_xml_depth、max_sheets、max_tables、max_rows、max_cells、max_shared_strings，均可在metadata.limits逐项覆盖。超过预算抛CatalogError，不做截断成功。

## 当前能力与配置

当前app.baseline.analyze只产生一份响应的概览，兼容入口继续保留；新采集可增加入口，不能把字节摘要等同于协议或业务完整性。
读取预算、授权范围和来源凭据属于调用配置，真实密钥不放在资料目录或交付证据中。
