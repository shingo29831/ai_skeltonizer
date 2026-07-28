# AI Role Comment: Filters file paths based on architectural layer rules to exclude presentation/UI layers and isolate domain logic.

from pathlib import Path
from typing import List, Set


DEFAULT_UI_EXTENSIONS: Set[str] = {
    '.tsx', '.jsx', '.css', '.scss', '.less', '.html', '.vue', '.svelte'
}

DEFAULT_UI_DIRECTORIES: Set[str] = {
    'components', 'views', 'pages', 'ui', 'styles', 'layouts', 'templates'
}


def filter_logic_files(
    file_paths: List[Path],
    exclude_extensions: Set[str] = DEFAULT_UI_EXTENSIONS,
    exclude_dirs: Set[str] = DEFAULT_UI_DIRECTORIES
) -> List[Path]:
    if not isinstance(file_paths, list):
        raise TypeError('file_paths must be a list of Path objects.')

    filtered_paths: List[Path] = []

    for path in file_paths:
        if not isinstance(path, Path):
            raise TypeError('All elements in file_paths must be Path instances.')

        # Normalizing path resolution prevents directory traversal discrepancies during filtering.
        resolved_path = path.resolve()

        if resolved_path.suffix.lower() in exclude_extensions:
            continue

        path_parts = {part.lower() for part in resolved_path.parts}
        if path_parts.intersection(exclude_dirs):
            continue

        filtered_paths.append(path)

    return filtered_paths