"""Generate client input templates with the application's existing XLSX writer."""
from app.xlsx_utils import read_workbook_bytes,write_workbook_bytes
from app.historical_onboarding import HistoricalProductOnboarding


def example_with_scores(raw,blank=False):
    sheets=read_workbook_bytes(raw)
    data=HistoricalProductOnboarding().analyze('example.xlsx',raw,'DEMO','演练示例',[],require_dual_header=True)['data']
    # Only the synthetic example is rewritten; customer files are read verbatim.
    score_rows=[['英文代号','属性名称','属性类型','属性分组','专家编号','价格重要性评分']]
    for i,p in enumerate(data['parameters']):
        score_rows.append([p['parameter_id'],p['label'],p['value_type'],
                           '' if blank else ('功能','性能','环境')[i%3],
                           '' if blank else '演练专家（非业务评分）',None if blank else 1+i%5])
    product=next((name,rows) for name,rows in sheets.items() if name=='成品数据')
    if blank:product=(product[0],product[1][:2])
    return write_workbook_bytes([product,('价格属性评分',score_rows)])
