"""Standard-library-only offline verifier; no server or credentials required."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ledgervigil.execution.proof_verify import main
if __name__=='__main__':raise SystemExit(main())
