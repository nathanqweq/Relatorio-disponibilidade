"""Deixa os testes importarem os programas da pasta servidor/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
