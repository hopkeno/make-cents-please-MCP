"""File access limited to the configured Charity Record folder."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..ynab.formatting import InputError

MAX_BYTES = 10 * 1024 * 1024  # Charity Record's own import limit
IMPORT_PREFIX = "charityrecord-import-"


class Folder:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, name: str) -> Path:
        """Resolve ``name`` inside the folder; refuse anything that escapes it."""
        candidate = (self.root / name).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise InputError(
                f"{name!r} is outside the Charity Record folder. Use a file name from "
                "charityrecord_list_files."
            )
        return candidate

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def csv_files(self) -> list[Path]:
        """CSV files in the folder and one level of subfolders (e.g. an unzipped export)."""
        found = list(self.root.glob("*.csv")) + list(self.root.glob("*/*.csv"))
        return sorted(p for p in found if p.is_file())

    def read(self, name: str) -> str:
        path = self.resolve(name)
        if not path.is_file():
            raise InputError(f"{name!r} was not found; use charityrecord_list_files.")
        if path.stat().st_size > MAX_BYTES:
            raise InputError(f"{name!r} is larger than 10 MB.")
        return path.read_text(encoding="utf-8-sig")

    def write_new(self, stem: str, text: str) -> str:
        """Write a new file, never overwriting; returns its name relative to the folder."""
        n = 1
        while True:
            name = f"{stem}.csv" if n == 1 else f"{stem}-{n}.csv"
            path = self.resolve(name)
            try:
                with path.open("x", encoding="utf-8", newline="") as fh:
                    fh.write(text)
                return name
            except FileExistsError:
                n += 1


def import_stem(kind: str, now: datetime) -> str:
    return f"{IMPORT_PREFIX}{now:%Y%m%d-%H%M%S}-{kind.replace('_', '-')}"
