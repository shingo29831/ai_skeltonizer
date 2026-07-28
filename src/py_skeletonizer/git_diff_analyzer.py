# AI Role Comment: Analyzes Git diffs and parses file imports to extract a minimal dependency graph of modified files and their direct dependencies.

import re
import subprocess
from pathlib import Path
from typing import List, Set


def get_staged_or_modified_files(repo_path: Path, timeout_sec: int = 10) -> Set[Path]:
    if not repo_path.exists() or not repo_path.is_dir():
        raise FileNotFoundError(f'Repository path does not exist: {repo_path}')

    try:
        # Setting a strict timeout prevents hanging subprocess calls when Git prompts for credentials or locks.
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout_sec
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Git command timed out.') from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f'Git command failed: {exc.stderr}') from exc

    modified_files: Set[Path] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        file_rel_path = line[3:].strip()
        
        # We ignore deleted files ('D') as they cannot be read or parsed for dependencies.
        if 'D' not in status:
            full_path = (repo_path / file_rel_path).resolve()
            if full_path.exists() and full_path.is_file():
                modified_files.add(full_path)

    return modified_files


def parse_direct_dependencies(target_files: Set[Path], all_project_files: Set[Path]) -> Set[Path]:
    dependencies: Set[Path] = set(target_files)
    
    # Pre-building a lookup map by stem avoids N+1 disk scans when matching import statements.
    file_map = {file.stem: file for file in all_project_files if file.is_file()}

    import_pattern = re.compile(r'^(?:from|import)\s+([a-zA-Z0-9_\.]+)')

    for target_file in target_files:
        try:
            content = target_file.read_text(encoding='utf-8')
        except OSError as exc:
            raise RuntimeError(f'Failed to read file for dependency parsing: {target_file}') from exc

        for line in content.splitlines():
            stripped = line.strip()
            match = import_pattern.match(stripped)
            if match:
                module_path = match.group(1)
                module_name = module_path.split('.')[0]
                if module_name in file_map:
                    dependencies.add(file_map[module_name])

    return dependencies