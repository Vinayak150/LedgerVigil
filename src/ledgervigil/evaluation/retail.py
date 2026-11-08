"""Pinned UCI Online Retail II inspection, preserving OOXML numeric lexemes.

This is an offline evaluation reader, not the native invoice importer. Quarantined
rows are retained. A positive row is only an invoice-line candidate, never a
payment, settlement, validated invoice or accounting accuracy label.
"""
from dataclasses import dataclass
from datetime import date,timedelta
from decimal import Decimal,InvalidOperation,localcontext
from pathlib import Path
import hashlib,json,re,zipfile
from xml.etree import ElementTree as ET
from ledger_kernel.money import Money,GBP,DomainError,CONTEXT

PROFILE='lv-uci-retail-ii-evaluation/1'
PUBLIC_SHA256='bcbe73b35f5b7babf197fb0cb983a11f5d9ff929078d4aa53d171b1f2df2e980'
NS='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
SHEETS=(('Year 2009-2010','xl/worksheets/sheet1.xml'),('Year 2010-2011','xl/worksheets/sheet2.xml'))
HEADERS=('Invoice','StockCode','Description','Quantity','InvoiceDate','Price','Customer ID','Country')
SOURCE={'title':'Online Retail II','creator':'Daqing Chen','citation':'Chen, D. (2012). Online Retail II [Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C5CG6D.',
 'url':'https://archive.ics.uci.edu/dataset/502/online+retail+ii','download_url':'https://archive.ics.uci.edu/static/public/502/online%2Bretail%2Bii.zip','doi':'10.24432/C5CG6D',
 'license':'CC BY 4.0','license_url':'https://creativecommons.org/licenses/by/4.0/','classification':'PUBLIC_OBSERVATIONAL','currency':'GBP',
 'settlement_ground_truth':False,'accounting_review':'UNREVIEWED'}

class WorkbookRejected(ValueError):pass

def canonical(v):return json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def file_hash(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  while chunk:=f.read(1024*1024):h.update(chunk)
 return h.hexdigest()

class SafeXML:
 """Reject declarations before they reach Expat, including chunk-split tokens."""
 def __init__(self,stream):self.stream=stream;self.tail=b''
 def read(self,n=-1):
  data=self.stream.read(65536 if n<0 else min(n,65536));scan=self.tail+data
  if b'<!DOCTYPE' in scan or b'<!ENTITY' in scan:raise WorkbookRejected('XML declarations unavailable in this profile')
  self.tail=scan[-16:];return data

@dataclass(frozen=True)
class SourceRow:
 sheet:str
 row:int
 values:tuple[str|None,...]
 issues:tuple[str,...]=()
 @property
 def payload(self):return {'values':self.values,'cell_issues':self.issues}
 @property
 def sha256(self):return hashlib.sha256(canonical(self.payload).encode()).hexdigest()

class Workbook:
 def __init__(self,path,expected_sha256):
  self.path=Path(path)
  if not re.fullmatch('[0-9a-f]{64}',expected_sha256) or file_hash(path)!=expected_sha256:raise WorkbookRejected('SOURCE_HASH_MISMATCH')
  self.sha256=expected_sha256
 def rows(self):
  with zipfile.ZipFile(self.path) as z:
   infos=z.infolist();names=[i.filename for i in infos]
   if len(names)!=len(set(names)) or len(infos)>200 or sum(i.file_size for i in infos)>1024**3:raise WorkbookRejected('archive inventory exceeds profile')
   if any(i.file_size>768*1024**2 or (i.file_size/max(1,i.compress_size))>2000 for i in infos):raise WorkbookRejected('archive member exceeds profile')
   def root(name):
    with z.open(name) as f:return ET.parse(SafeXML(f)).getroot()
   wb=root('xl/workbook.xml');properties=wb.find(NS+'workbookPr')
   if properties is not None and properties.get('date1904') in ('1','true'):raise WorkbookRejected('1904 dates unavailable')
   relations={e.get('Id'):e.get('Target') for e in root('xl/_rels/workbook.xml.rels')}
   if any(e.get('TargetMode')=='External' for e in root('xl/_rels/workbook.xml.rels')):raise WorkbookRejected('external relationships unavailable')
   actual=tuple((s.get('name'),'xl/'+relations.get(s.get(REL),'').lstrip('/')) for s in wb.findall(NS+'sheets/'+NS+'sheet'))
   if actual!=SHEETS:raise WorkbookRejected('unexpected workbook sheet layout')
   if z.getinfo('xl/sharedStrings.xml').file_size>64*1024**2:raise WorkbookRejected('shared strings exceed profile')
   strings=[]
   with z.open('xl/sharedStrings.xml') as f:
    container=None
    for event,e in ET.iterparse(SafeXML(f),events=('start','end')):
     if container is None:container=e
     if event=='end' and e.tag==NS+'si':
      strings.append(''.join(t.text or '' for t in e.iter(NS+'t')))
      if len(strings)>500000 or len(strings[-1])>32767:raise WorkbookRejected('shared strings exceed profile')
      container.remove(e);e.clear()
   for sheet,name in SHEETS:
    previous=0;sheet_data=None;header=False
    with z.open(name) as f:
     for event,e in ET.iterparse(SafeXML(f),events=('start','end')):
      if event=='start' and e.tag==NS+'sheetData':sheet_data=e
      if event!='end' or e.tag!=NS+'row':continue
      number=int(e.get('r','0'))
      if number<=previous:raise WorkbookRejected('row coordinates not increasing')
      previous=number;values=[None]*8;seen=set();issues=set()
      for cell in e.findall(NS+'c'):
       match=re.fullmatch(r'([A-H])([1-9][0-9]*)',cell.get('r',''))
       if not match or int(match[2])!=number or match[1] in seen:raise WorkbookRejected('unexpected cell coordinates')
       seen.add(match[1]);typ=cell.get('t','n');raw=cell.findtext(NS+'v')
       if cell.find(NS+'f') is not None:issues.add('FORMULA_CELL')
       if typ=='s':
        try:raw=strings[int(raw)] if raw is not None and int(raw)>=0 else None
        except (ValueError,IndexError):raise WorkbookRejected('bad shared string reference') from None
       elif typ=='inlineStr':raw=''.join(t.text or '' for t in cell.iter(NS+'t'))
       elif typ!='n':issues.add('UNSUPPORTED_CELL_TYPE')
       values[ord(match[1])-65]=raw
      if any(v not in (None,'') for v in values):
       if not header:
        if number!=1 or tuple(values)!=HEADERS or issues:raise WorkbookRejected('unexpected source columns')
        header=True
       else:yield SourceRow(sheet,number,tuple(values),tuple(sorted(issues)))
      if sheet_data is not None:sheet_data.remove(e)
      e.clear()
    if not header:raise WorkbookRejected('missing sheet header')


def assess(row:SourceRow):
 invoice,stock,description,quantity,serial,price,customer,country=row.values
 reasons=set(row.issues);qty=None;unit=None;extended=None;day=None
 if not invoice:reasons.add('MISSING_INVOICE')
 elif invoice.upper().startswith('C'):reasons.add('CANCELLATION_REQUIRES_ORIGINAL_LINK')
 elif not re.fullmatch(r'[0-9]{6}',invoice):reasons.add('NON_SALE_INVOICE_CODE')
 if not stock:reasons.add('MISSING_STOCK_CODE')
 if not description or not description.strip():reasons.add('MISSING_DESCRIPTION')
 if not customer:reasons.add('MISSING_CUSTOMER_ID')
 elif not re.fullmatch(r'[0-9]{5}',customer):reasons.add('INVALID_CUSTOMER_ID')
 if not country or not country.strip():reasons.add('MISSING_COUNTRY')
 if not quantity or not re.fullmatch(r'-?[0-9]{1,12}',quantity):reasons.add('INVALID_QUANTITY')
 else:
  qty=int(quantity)
  if qty<=0:reasons.add('NONPOSITIVE_QUANTITY')
 try:
  p=Decimal(price or '')
  with localcontext(CONTEXT):
   money=Money(p,GBP);unit=int(money.value*100)
   if p<=0:reasons.add('NONPOSITIVE_UNIT_PRICE')
   if qty is not None:extended=int(Money(p*qty,GBP).value*100)
 except (InvalidOperation,DomainError,ValueError):reasons.add('INVALID_OR_SUB_MINOR_MONEY')
 try:
  v=Decimal(serial or '')
  if not v.is_finite() or not 61<=v<2958466:raise ValueError('date range')
  day=(date(1899,12,30)+timedelta(days=int(v))).isoformat()
 except (InvalidOperation,ValueError,OverflowError):reasons.add('INVALID_DATE')
 return {'invoice':invoice,'customer':customer,'country':country,'invoice_date':day,'quantity':qty,'unit_minor':unit,'extended_minor':extended,'reasons':sorted(reasons),'candidate':not reasons}
