"""Entry used by the native Win32 launcher and diagnostic console scripts."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from product.launcher import entry

if __name__=='__main__':raise SystemExit(entry())
