from __future__ import annotations

from pathlib import Path

from setuptools import setup

ROOT = Path(__file__).parent
version_ns: dict[str, str] = {}
exec((ROOT / "lab_kit" / "__init__.py").read_text(encoding="utf-8"), version_ns)


setup(
    name="labkit-cli",
    version=version_ns["__version__"],
    description="Safety-first CLI for managing Codex CLI and Claude Code experimental controls.",
    long_description=(ROOT / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    python_requires=">=3.9",
    packages=["lab_kit"],
    include_package_data=True,
    package_data={"lab_kit": ["data/*.json"]},
    install_requires=[],
    extras_require={"dev": ["black>=24.0", "mypy>=1.8", "ruff>=0.5"]},
    entry_points={"console_scripts": ["labkit=lab_kit.cli:main"]},
)
