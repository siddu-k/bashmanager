import os
import re
from pathlib import Path

def validate_safe_path(base_dir, rel_path):
    """
    Validate that rel_path is safe and resolves to a path inside base_dir.
    Raises ValueError if path traversal or unsafe access is detected.
    """
    if not rel_path:
        raise ValueError("Path cannot be empty")
        
    base_path = Path(base_dir).resolve()
    # Join and resolve path
    target_path = Path(os.path.join(base_dir, rel_path)).resolve()
    
    # Check that target_path is inside base_path
    if not str(target_path).startswith(str(base_path)):
        raise ValueError("Invalid path: Path traversal detected")
        
    return target_path

def validate_git_branch(branch_name):
    """
    Validate git branch name rules to prevent command injection and ensure git compatibility.
    Raises ValueError if branch name is invalid.
    """
    if not branch_name:
        raise ValueError("Branch name cannot be empty")
        
    # Git branch name security rules
    if branch_name.startswith('-'):
        raise ValueError(f"Invalid branch name: cannot start with dash")
        
    # Only allow safe characters: alphanumeric, dashes, underscores, slashes, dots
    if not re.match(r'^[a-zA-Z0-9._/-]+$', branch_name):
        raise ValueError(f"Invalid branch name: {branch_name}")
        
    if '..' in branch_name:
        raise ValueError("Invalid branch name: cannot contain '..'")
        
    if branch_name.endswith('.lock'):
        raise ValueError("Invalid branch name: cannot end with '.lock'")
        
    if branch_name.endswith('/'):
        raise ValueError("Invalid branch name: cannot end with '/'")
        
    return branch_name

def validate_repo_name(repo_name):
    """
    Validate repository name/URL to ensure safe git push commands.
    Raises ValueError if repository name/URL contains invalid/unsafe characters.
    """
    if not repo_name:
        raise ValueError("Repository name/URL cannot be empty")
        
    repo_name = repo_name.strip()
    
    if repo_name.startswith('-'):
        raise ValueError("Invalid repository URL/name: cannot start with dash")
        
    # Allow safe URL and remote name characters (alphanumeric, colons, slashes, dots, dashes, underscores, @, +)
    if not re.match(r'^[a-zA-Z0-9._/:-@+]+$', repo_name):
        raise ValueError(f"Invalid repository URL/name: {repo_name}")
        
    return repo_name
