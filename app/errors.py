"""采集失败及失败位置。"""

class CatalogError(ValueError):
    """带结构化失败位置与上下文的采集错误。

    location 始终为字典，至少保留部件；关系、工作表、表对象、行列及
    单元格坐标在知道时一并保留，调用方无需从消息文本中还原位置。
    """

    def __init__(self,code,message,location=None,**details):
        super().__init__(message)
        self.code=code
        self.message=message
        self.location=dict(location or {})
        self.details=details

    def as_dict(self):
        problem={'code':self.code,'message':self.message,'location':self.location}
        problem.update(self.details)
        return {'error':problem}
