"""PyInstaller entry point. Deliberately imports no model or training modules."""
from product.launcher import entry

if __name__ == '__main__':
    raise SystemExit(entry())
