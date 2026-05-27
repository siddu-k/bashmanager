from pathlib import Path
import re
import urllib.parse


def validate_safe_path(base_dir: str, rel_path: str) -> Path:
    """
    Validates that a resolved path is within the base directory.
    Uses Path.relative_to() to prevent path traversal attacks.
    """
    base = Path(base_dir).resolve()
    target = (base / rel_path).resolve()

    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError("Invalid path: traversal attempt detected")

    return target


def validate_git_branch(branch_name: str) -> str:
    """
    Validates a Git branch name according to strict security rules.
    Prevents argument injection and invalid references.
    """
    if not branch_name:
        raise ValueError("Branch name cannot be empty")

    # Basic strict character set validation
    if not re.match(r"^[a-zA-Z0-9._/-]+$", branch_name):
        raise ValueError("Invalid characters in branch name")

    # Prevent argument injection
    if branch_name.startswith("-") or branch_name.startswith("--"):
        raise ValueError("Branch name cannot start with '-' or '--'")

    # Git ref specific validation rules
    invalid_patterns = [
        "..",
        "@{",
        "//",
    ]
    for pattern in invalid_patterns:
        if pattern in branch_name:
            raise ValueError(f"Branch name contains invalid pattern: {pattern}")

    if branch_name.endswith(".") or branch_name.endswith("/"):
        raise ValueError("Branch name cannot end with '.' or '/'")

    # The regex already prevents spaces, but this is an extra sanity check
    if " " in branch_name:
        raise ValueError("Branch name cannot contain spaces")

    return branch_name


def validate_repo_name(repo_url: str) -> str:
    """
    Validates a repository URL to ensure it is a safe GitHub URL.
    Only allows GitHub HTTPS or SSH formats.
    """
    if not repo_url:
        raise ValueError("Repository URL cannot be empty")

    # Check for valid SSH format: git@github.com:user/repo.git
    if repo_url.startswith("git@github.com:"):
        # Additional sanity checks on SSH format
        if " " in repo_url or ";" in repo_url or "&" in repo_url:
            raise ValueError("Invalid characters in SSH repository URL")
        return repo_url

    # Check for HTTPS format
    try:
        parsed = urllib.parse.urlparse(repo_url)
    except Exception:
        raise ValueError("Invalid repository URL format")

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Invalid URL scheme '{parsed.scheme}'. Only HTTPS is allowed."
        )

    if parsed.netloc not in ("github.com", "www.github.com"):
        raise ValueError("Only GitHub URLs are allowed")

    if " " in repo_url or ";" in repo_url or "&" in repo_url:
        raise ValueError("Invalid characters in repository URL")

    return repo_url
