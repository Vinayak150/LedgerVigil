"""Verify an entire offline admission plan before showing a source invoice.

No server, database mutation, financial action or accounting decision. Pin the
manifest hash from a trusted review package for authenticity as well as integrity.
"""
import argparse,json
from pathlib import Path
from ledgervigil.evaluation.admission import verify_plan


def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--plan',type=Path,required=True);parser.add_argument('--manifest-sha256');parser.add_argument('--invoice');parser.add_argument('--lines',type=int,default=10);args=parser.parse_args()
 if not 0<=args.lines<=100:parser.error('--lines must be between 0 and 100')
 verified=verify_plan(args.plan,expected_manifest_sha256=args.manifest_sha256,select_invoice=args.invoice)
 result={k:v for k,v in verified.items() if k!='selected'}
 if args.invoice:
  if verified['selected'] is None:parser.exit(2,'Invoice not found in the fully verified plan\n')
  selected=verified['selected'];lines=selected['source_rows']
  result['invoice']={k:v for k,v in selected.items() if k!='source_rows'}
  result['invoice']['source_rows_shown']=lines[:args.lines];result['invoice']['additional_source_rows_not_shown']=max(0,len(lines)-args.lines)
 print(json.dumps(result,indent=2,ensure_ascii=False))

if __name__=='__main__':main()
