from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import sys
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

# Files that are useful for an LLM to understand the project.
ALLOWED_EXTENSIONS = {
    # Python
    ".py",

    # JavaScript / TypeScript
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",

    # Web
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",

    # Config / data
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",

    # SQL / migrations
    ".sql",

    # Shell
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",

    # Documentation
    ".md",
    ".txt",
}


# Directories that should never be extracted.
EXCLUDED_DIRS = {
    "__pycache__",
    ".git",
    ".github",
    ".idea",
    ".vscode",

    "venv",
    ".venv",
    "env",
    ".env",

    "node_modules",

    ".next",
    "dist",
    "build",
    "out",

    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",

    "coverage",
    ".coverage",

    "logs",
    "tmp",
    "temp",

    "site-packages",

    "htmlcov",
    "extracted_Codes",
}


# Specific files that should never be included.
EXCLUDED_FILES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.staging",

    ".gitignore",

    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",

    "poetry.lock",

    # Don't recursively include previously generated extraction files.
    "codes.txt",
}


# Patterns for sensitive files.
SENSITIVE_FILE_PREFIXES = {
    ".env.",
}


# Maximum individual source file size.
# Prevents accidentally dumping giant generated files.
MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB


# ============================================================
# HELPERS
# ============================================================

def should_exclude_directory(path: Path) -> bool:
    """Return True if directory should not be scanned."""

    return path.name.lower() in {
        name.lower() for name in EXCLUDED_DIRS
    }


def should_exclude_file(path: Path) -> bool:
    """Return True if a file should not be included."""

    name = path.name
    lower_name = name.lower()

    # Exact excluded files
    if lower_name in {
        item.lower() for item in EXCLUDED_FILES
    }:
        return True

    # Generated output files
    if lower_name.endswith("_codes.txt"):
        return True

    # Environment files
    if lower_name == ".env":
        return True

    if lower_name.startswith(".env."):
        return True

    # Python bytecode
    if path.suffix.lower() in {
        ".pyc",
        ".pyo",
    }:
        return True

    return False


def is_allowed_source_file(path: Path) -> bool:
    """Check if this file should be included."""

    if not path.is_file():
        return False

    if should_exclude_file(path):
        return False

    if path.suffix.lower() not in ALLOWED_EXTENSIONS:
        return False

    try:
        if path.stat().st_size > MAX_FILE_SIZE_BYTES:
            print(
                f"[SKIP] File too large: {path}"
            )
            return False
    except OSError:
        return False

    return True


def normalise_cli_target(raw_target: str) -> str:
    """
    Supports both:

        python extract.py app/api/auth

    and:

        python extract.py --app/api/auth

        python extract.py --app
    """

    raw_target = raw_target.strip()

    if raw_target.startswith("--"):
        raw_target = raw_target[2:]

    # Allow forward slash syntax on Windows.
    raw_target = raw_target.replace("/", os.sep)

    return raw_target


def resolve_target(raw_target: str) -> Path:
    """Resolve requested path intelligently."""

    cleaned = normalise_cli_target(raw_target)

    if cleaned in {
        "",
        ".",
    }:
        return Path.cwd().resolve()

    candidate = Path(cleaned)

    # First try relative to current directory.
    if candidate.exists():
        return candidate.resolve()

    # Check whether user typed current root folder name.
    cwd = Path.cwd()

    if cleaned.lower() == cwd.name.lower():
        return cwd.resolve()

    # Check parent directory.
    parent_candidate = cwd.parent / cleaned

    if parent_candidate.exists():
        return parent_candidate.resolve()

    raise FileNotFoundError(
        f"Target not found: {raw_target}"
    )


def collect_source_files(target: Path) -> list[Path]:
    """Recursively collect source files."""

    if target.is_file():
        if is_allowed_source_file(target):
            return [target]

        raise ValueError(
            f"Unsupported or excluded file: {target}"
        )

    collected: list[Path] = []

    for root, dirs, files in os.walk(target):
        root_path = Path(root)

        # Prevent traversal into excluded directories.
        dirs[:] = [
            directory
            for directory in dirs
            if not should_exclude_directory(
                root_path / directory
            )
        ]

        for file_name in files:
            file_path = root_path / file_name

            if is_allowed_source_file(file_path):
                collected.append(file_path)

    # Stable output ordering makes diffs easier.
    return sorted(
        collected,
        key=lambda p: str(p).lower(),
    )


def read_text_file(path: Path) -> str | None:
    """
    Read text safely.

    UTF-8 is attempted first and common Windows fallback second.
    """

    encodings = [
        "utf-8",
        "utf-8-sig",
        "cp1252",
    ]

    for encoding in encodings:
        try:
            return path.read_text(
                encoding=encoding
            )
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            print(
                f"[ERROR] Could not read {path}: {exc}"
            )
            return None

    print(
        f"[SKIP] Unable to decode: {path}"
    )

    return None


def get_display_path(
    file_path: Path,
    target: Path,
) -> str:
    """Create an LLM-friendly relative path."""

    if target.is_file():
        return file_path.name

    try:
        relative = file_path.relative_to(target)
    except ValueError:
        relative = file_path

    return relative.as_posix()

def generate_tree(target: Path) -> str:
    """
    Generate a tree structure for the target directory.
    Excluded directories/files are ignored.
    """

    lines: list[str] = []

    root_name = target.name if target.name else str(target)
    lines.append(f"{root_name}/")

    def walk(directory: Path, prefix: str = "") -> None:
        entries = []

        try:
            for entry in directory.iterdir():
                if entry.is_dir():
                    if should_exclude_directory(entry):
                        continue
                elif entry.is_file():
                    if should_exclude_file(entry):
                        continue

                entries.append(entry)

        except PermissionError:
            return

        entries.sort(
            key=lambda p: (
                not p.is_dir(),
                p.name.lower(),
            )
        )

        for index, entry in enumerate(entries):
            is_last = index == len(entries) - 1

            connector = "└── " if is_last else "├── "

            if entry.is_dir():
                lines.append(
                    f"{prefix}{connector}{entry.name}/"
                )
            else:
                lines.append(
                    f"{prefix}{connector}{entry.name}"
                )

            if entry.is_dir():
                extension = "    " if is_last else "│   "

                walk(
                    entry,
                    prefix + extension,
                )

    if target.is_dir():
        walk(target)

    return "\n".join(lines)

def create_tree_output_filename(target: Path) -> Path:
    if target.is_file():
        base_name = target.stem
    else:
        base_name = target.name

    if not base_name:
        base_name = "project"

    safe_name = "".join(
        char if char.isalnum() or char in "-_"
        else "_"
        for char in base_name
    )

    output_dir = Path.cwd() / "extracted_Codes"
    output_dir.mkdir(parents=True, exist_ok=True)

    ist_now = datetime.now(
        ZoneInfo("Asia/Kolkata")
    )

    timestamp = ist_now.strftime(
        "%d-%m-%Y_%I-%M_%p"
    )

    filename = (
        f"{safe_name}_tree_{timestamp}.txt"
    )

    return output_dir / filename

def export_tree(target: Path) -> None:
    if not target.is_dir():
        raise ValueError(
            "--tree requires a directory."
        )

    tree_text = generate_tree(target)

    output_file = create_tree_output_filename(
        target
    )

    output_file.write_text(
        tree_text,
        encoding="utf-8",
    )

    print()
    print("=" * 60)
    print("TREE EXTRACTION COMPLETE")
    print("=" * 60)
    print(f"Source : {target}")
    print(f"Output : {output_file}")
    print("=" * 60)


def create_output_filename(target: Path) -> Path:
    """
    Creates output inside extracted_Codes/ with IST timestamp.

    Example:
        auth_codes_22-08-2026_11-15_AM.txt
    """

    if target.is_file():
        base_name = target.stem
    else:
        base_name = target.name

    if not base_name:
        base_name = "project"

    safe_name = "".join(
        char if char.isalnum() or char in "-_"
        else "_"
        for char in base_name
    )

    # Create output directory
    output_dir = Path.cwd() / "extracted_Codes"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Current IST date/time
    ist_now = datetime.now(ZoneInfo("Asia/Kolkata"))

    timestamp = ist_now.strftime("%d-%m-%Y_%I-%M_%p")

    filename = f"{safe_name}_codes_{timestamp}.txt"

    return output_dir / filename

# ============================================================
# EXTRACTION
# ============================================================

def extract_project(
    target: Path,
    output_file: Path,
) -> None:

    source_files = collect_source_files(target)

    if not source_files:
        print(
            f"No supported source files found in: {target}"
        )
        return

    output_parts: list[str] = []

    # --------------------------------------------------------
    # Header
    # --------------------------------------------------------

    output_parts.append(
        "=" * 90
    )
    output_parts.append(
        "PROJECT SOURCE CODE EXPORT"
    )
    output_parts.append(
        "=" * 90
    )

    output_parts.append(
        f"Source: {target}"
    )

    output_parts.append(
        f"Files: {len(source_files)}"
    )

    output_parts.append("")

    # --------------------------------------------------------
    # Project file index
    # --------------------------------------------------------

    output_parts.append(
        "=" * 90
    )
    output_parts.append(
        "FILE INDEX"
    )
    output_parts.append(
        "=" * 90
    )
    output_parts.append("")

    for file_path in source_files:
        display_path = get_display_path(
            file_path,
            target,
        )

        output_parts.append(
            display_path
        )

    output_parts.append("")

    # --------------------------------------------------------
    # File contents
    # --------------------------------------------------------

    for index, file_path in enumerate(
        source_files,
        start=1,
    ):
        content = read_text_file(file_path)

        if content is None:
            continue

        display_path = get_display_path(
            file_path,
            target,
        )

        output_parts.extend(
            [
                "",
                "=" * 90,
                f"FILE {index}/{len(source_files)}",
                f"PATH: {display_path}",
                "=" * 90,
                "",
                content.rstrip(),
                "",
                f"END FILE: {display_path}",
                "=" * 90,
                "",
            ]
        )

    final_output = "\n".join(
        output_parts
    )

    output_file.write_text(
        final_output,
        encoding="utf-8",
    )

    # --------------------------------------------------------
    # Result
    # --------------------------------------------------------

    total_size = output_file.stat().st_size

    print()
    print("=" * 60)
    print("SOURCE EXTRACTION COMPLETE")
    print("=" * 60)

    print(
        f"Source       : {target}"
    )

    print(
        f"Files        : {len(source_files)}"
    )

    print(
        f"Output       : {output_file}"
    )

    print(
        f"Output size  : {total_size / 1024:.2f} KB"
    )

    print("=" * 60)


# ============================================================
# CLI
# ============================================================

def print_help() -> None:

    script_name = Path(
        sys.argv[0]
    ).name

    print(
        f"""
Project Source Code Extractor

Usage:

    python {script_name} <folder>

Examples:

    python {script_name} app/api/auth
    python {script_name} app/api/v1
    python {script_name} app
    python {script_name} .
    python {script_name} backend_freight

Alternative syntax:

    python {script_name} --app/api/auth
    python {script_name} --app/api/v1
    python {script_name} --app
    python {script_name} --backend_freight

Output examples:

    app/api/auth
        -> auth_codes.txt

    app
        -> app_codes.txt

    backend_freight
        -> backend_freight_codes.txt
"""
    )


def main() -> None:

    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)

    argument = sys.argv[1]
    tree_mode = "--tree" in sys.argv[2:]

    if argument in {
        "-h",
        "--help",
        "help",
    }:
        print_help()
        return

    try:
        target = resolve_target(argument)

        if tree_mode:
            export_tree(target)
            return

        output_file = create_output_filename(
            target
        )

        # Prevent output being included in itself.
        if (
            target.is_file()
            and target.resolve()
            == output_file.resolve()
        ):
            raise ValueError(
                "Output file cannot be the source file."
            )

        extract_project(
            target,
            output_file,
        )

    except FileNotFoundError as exc:
        print(
            f"[ERROR] {exc}"
        )
        sys.exit(1)

    except ValueError as exc:
        print(
            f"[ERROR] {exc}"
        )
        sys.exit(1)

    except KeyboardInterrupt:
        print(
            "\nExtraction cancelled."
        )
        sys.exit(130)


if __name__ == "__main__":
    main()