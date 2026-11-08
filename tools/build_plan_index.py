"""Build the pinned public-plan index offline; no financial database or server writes."""
import argparse,json
from pathlib import Path
from ledgervigil.ingestion.plan_index import build_index

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();print(json.dumps(build_index(a.plan,a.output),indent=2))
if __name__=='__main__':main()
