from pathlib import Path
import setuptools

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "backend"

with open(BACKEND_DIR / "VERSION", encoding="utf-8") as f:
    version = f.read().strip()

with open(BASE_DIR / "README.md", encoding="utf-8") as f:
    long_description = f.read()

def parse_requirements(lines):
    return [
        line.split(" ")[0]
        for line in lines
        if line and not line.startswith("#")
    ]

with open(BACKEND_DIR / "requirements" / "common.txt", encoding="utf-8") as f:
    run_packages = parse_requirements(f.read().splitlines())

with open(BACKEND_DIR / "requirements" / "youtube.txt", encoding="utf-8") as f:
    run_packages.extend(parse_requirements(f.read().splitlines()))

with open(BACKEND_DIR / "requirements" / "install.txt", encoding="utf-8") as f:
    install_packages = parse_requirements(f.read().splitlines())

with open(BACKEND_DIR / "requirements" / "screenvis.txt", encoding="utf-8") as f:
    screenvis_packages = parse_requirements(f.read().splitlines())

setuptools.setup(
    name="raveberry",
    version=version,
    author="Jasmin Hacker",
    author_email="raveberry@jhacker.de",
    description="A multi-user music server with a focus on participation",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/raveberry/raveberry",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Framework :: Django",
        "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)",
        "Programming Language :: Python :: 3",
    ],
    packages=[
        "raveberry",
        "raveberry.core",
        "raveberry.main",
        "raveberry.tests",
        "raveberry.visualizer",
    ],
    package_dir={
        "raveberry": "backend",
        "raveberry.core": "backend/core",
        "raveberry.main": "backend/main",
        "raveberry.tests": "backend/tests",
        "raveberry.visualizer": "backend/visualizer",
    },
    include_package_data=True,
    python_requires=">=3.8",
    extras_require={
        "install": install_packages,
        "run": run_packages,
        "screenvis": screenvis_packages,
        "visualizer": [
            "numpy>=1.23,<3",
            "aiohttp>=3.9,<4",
            "PyAudioWPatch==0.2.12.9;sys_platform=='win32'",
        ],
    },
    scripts=["bin/raveberry"],
)
