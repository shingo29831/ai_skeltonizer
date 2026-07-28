# AI Role Comment: Separates project files into static skeleton context (types, schemas) and dynamic implementation context.

from pathlib import Path
from typing import Dict, List, Set


DEFAULT_TYPE_PATTERNS: Set[str] = {
    'types.ts', 'type.ts', 'interfaces.ts', 'schema.ts', 'schemas.ts',
    'models.py', 'schema.py', 'types.py'
}


def is_static_skeleton_file(file_path: Path, custom_patterns: Set[str] = DEFAULT_TYPE_PATTERNS) -> bool:
    if not isinstance(file_path, Path):
        raise TypeError('file_path must be a Path object.')

    file_name = file_path.name.lower()
    if file_name in custom_patterns:
        return True

    # Matching generic suffixes isolates type definition files across TypeScript and Python conventions.
    if file_name.endswith('.d.ts') or file_name.endswith('_types.py') or file_name.endswith('_schema.py'):
        return True

    return False


def split_context_files(all_files: List[Path]) -> Dict[str, List[Path]]:
    if not isinstance(all_files, list):
        raise TypeError('all_files must be a list of Path objects.')

    static_files: List[Path] = []
    dynamic_files: List[Path] = []

    for path in all_files:
        if not isinstance(path, Path):
            raise TypeError('All elements in all_files must be Path instances.')

        if is_static_skeleton_file(path):
            static_files.append(path)
        else:
            dynamic_files.append(path)

    return {
        'static_skeleton': static_files,
        'dynamic_flesh': dynamic_files
    }