"""Explicit operator provisioning only; never invoked by API, worker or verifier."""
import argparse
from pathlib import Path
from ledgervigil.proofpacks.anchors import GitAnchors
from ledgervigil.proofpacks.integrity import IntegrityError

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('path',type=Path);a=p.parse_args()
 try:GitAnchors(a.path).initialize()
 except (OSError,IntegrityError) as exc:p.exit(1,str(exc)+'\n')
 print('Empty SHA-256 anchor repository initialized. Never use this to replace missing history.')
if __name__=='__main__':main()
