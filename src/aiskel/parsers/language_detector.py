# src/aiskel/parsers/language_detector.py
from pathlib import Path
from typing import Optional
from .base_parser import BaseParser
from .python_parser import PythonParser
from .javascript_parser import JavascriptParser

def get_parser_for_file(file_path: Path) -> Optional[BaseParser]:
    ext = file_path.suffix.lower()
    if ext == '.py':
        return PythonParser()
    elif ext in {'.js', '.jsx', '.ts', '.tsx'}:
        is_ts = ext in {'.ts', '.tsx'}
        is_tsx = ext in {'.jsx', '.tsx'}
        return JavascriptParser(is_typescript=is_ts, is_tsx=is_tsx)
    return None