"""Read the sheets of an .xlsx file with the standard library only (zipfile + XML).

Used to turn the downloaded food composition tables into the CSVs in reference/food_tables/ (D18).
The downloads are untrusted files, so every limit is enforced on what is actually unpacked, not on the sizes
the file claims, and cell positions are checked before anything is allocated for them. XML entity attacks
are refused by the expat parser Python ships with (Python docs, "XML security").
"""
import re
import zipfile
import xml.etree.ElementTree as ET

MAX_MEMBER_BYTES = 200 * 1024 * 1024    # one unpacked part (e.g. one sheet)
MAX_TOTAL_BYTES = 500 * 1024 * 1024     # everything unpacked from one workbook
MAX_COLUMNS = 16_384                    # Excel's own limit (column XFD)
MAX_ROWS = 1_048_576                    # Excel's own limit
MAX_CELLS = 5_000_000                   # cells (empty ones included) returned per sheet; the real tables use < 0.5 M

_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REF = re.compile(r"([A-Z]{1,3})([0-9]{1,7})")


class WorkbookError(ValueError):
    pass


def _col(ref):
    m = _REF.fullmatch(ref or "")
    if not m:
        raise WorkbookError("a cell has an unreadable position")
    n = 0
    for ch in m.group(1):
        n = n * 26 + ord(ch) - 64
    if n > MAX_COLUMNS or int(m.group(2)) > MAX_ROWS:
        raise WorkbookError("a cell sits outside the largest possible sheet")
    return n - 1


class Workbook:
    def __init__(self, path):
        try:
            self.zip = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile):
            raise WorkbookError("the file isn't a readable .xlsx workbook") from None
        self.unpacked = 0
        self.shared = []
        try:
            if "xl/sharedStrings.xml" in self.zip.namelist():
                root = ET.fromstring(self._read("xl/sharedStrings.xml"))
                self.shared = ["".join(t.text or "" for t in si.iter(_M + "t"))
                               for si in root.findall(_M + "si")]
            rels = ET.fromstring(self._read("xl/_rels/workbook.xml.rels"))
            target = {r.get("Id"): r.get("Target") for r in rels}
            sheets = ET.fromstring(self._read("xl/workbook.xml")).find(_M + "sheets")
        except ET.ParseError:
            raise WorkbookError("the workbook's structure isn't valid XML") from None
        if sheets is None:
            raise WorkbookError("the workbook lists no sheets")
        self.sheets = {}
        for s in sheets:
            t = (target.get(s.get(_R + "id")) or "").lstrip("/")
            self.sheets[s.get("name")] = t if t.startswith("xl/") else "xl/" + t

    def _read(self, member):
        """Unpack one part, stopping as soon as the real output passes a limit (sizes in the file can lie)."""
        try:
            f = self.zip.open(member)
        except KeyError:
            raise WorkbookError(f"the workbook has no part named {member[:60]!r}") from None
        except (zipfile.BadZipFile, NotImplementedError, OSError):
            raise WorkbookError("the workbook is damaged") from None
        chunks, size = [], 0
        try:
            with f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    self.unpacked += len(chunk)
                    if size > MAX_MEMBER_BYTES or self.unpacked > MAX_TOTAL_BYTES:
                        raise WorkbookError("the workbook is too large once unpacked")
                    chunks.append(chunk)
        except (zipfile.BadZipFile, OSError, EOFError):
            raise WorkbookError("the workbook is damaged") from None
        return b"".join(chunks)

    def rows(self, sheet):
        """Every non-empty row of a sheet as a list of cell texts (None for empty cells)."""
        if sheet not in self.sheets:
            raise WorkbookError(f"the workbook has no sheet named {sheet[:60]!r}")
        try:
            root = ET.fromstring(self._read(self.sheets[sheet]))
        except ET.ParseError:
            raise WorkbookError("a sheet isn't valid XML") from None
        allocated = 0
        for row in root.iter(_M + "row"):
            cells = {}
            for c in row.findall(_M + "c"):
                v = c.find(_M + "v")
                if v is None:
                    inline = c.find(_M + "is")
                    val = "".join(t.text or "" for t in inline.iter(_M + "t")) if inline is not None else None
                elif c.get("t") == "s":
                    try:
                        val = self.shared[int(v.text)]
                    except (ValueError, TypeError, IndexError):
                        raise WorkbookError("a cell points at a missing shared string") from None
                else:
                    val = v.text
                cells[_col(c.get("r"))] = val
            if cells:
                allocated += max(cells) + 1   # a row is as wide as its last cell, empty cells included
                if allocated > MAX_CELLS:
                    raise WorkbookError("a sheet has more cells than any food table needs")
                yield [cells.get(i) for i in range(max(cells) + 1)]
