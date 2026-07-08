"""Unpack Office files (DOCX, PPTX, XLSX) for editing.

Extracts the ZIP archive, pretty-prints XML files, and optionally:
- Merges adjacent runs with identical formatting (DOCX only)
- Simplifies adjacent tracked changes from same author (DOCX only)

Usage:
    python unpack.py <office_file> <output_dir> [options]

Examples:
    python unpack.py document.docx unpacked/
    python unpack.py presentation.pptx unpacked/
    python unpack.py document.docx unpacked/ --merge-runs false
"""

import argparse
import sys
import zipfile
from pathlib import Path

import defusedxml.minidom

from helpers.merge_runs import merge_runs as do_merge_runs
from helpers.simplify_redlines import simplify_redlines as do_simplify_redlines

SMART_QUOTE_REPLACEMENTS = {
    "\u201c": "&#x201C;",
    "\u201d": "&#x201D;",
    "\u2018": "&#x2018;",
    "\u2019": "&#x2019;",
}

# Office files decompress at most a few MB in practice. A crafted archive can
# claim a far larger uncompressed size (a zip bomb) while staying under any
# reasonable upload-size limit \u2014 cap the total before extracting.
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MB


def unpack(
    input_file: str,
    output_directory: str,
    merge_runs: bool = True,
    simplify_redlines: bool = True,
) -> tuple[None, str]:
    input_path = Path(input_file)
    output_path = Path(output_directory)
    suffix = input_path.suffix.lower()

    if not input_path.exists():
        return None, f"Error: {input_file} does not exist"

    if suffix not in {".docx", ".pptx", ".xlsx"}:
        return None, f"Error: {input_file} must be a .docx, .pptx, or .xlsx file"

    try:
        output_path.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(input_path, "r") as zf:
            total_uncompressed = sum(info.file_size for info in zf.infolist())
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                return None, (
                    f"Error: {input_file} would decompress to "
                    f"{total_uncompressed / (1024 * 1024):.0f} MB, over the "
                    f"{MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB limit"
                )
            zf.extractall(output_path)

        xml_files = list(output_path.rglob("*.xml")) + list(output_path.rglob("*.rels"))
        for xml_file in xml_files:
            _pretty_print_xml(xml_file)  # raises on malformed/DTD-bearing XML — never silently skip

        message = f"Unpacked {input_file} ({len(xml_files)} XML files)"

        if suffix == ".docx":
            if simplify_redlines:
                simplify_count, simplify_msg = do_simplify_redlines(str(output_path))
                if simplify_msg.startswith("Error"):
                    return None, f"Error unpacking: {simplify_msg}"
                message += f", simplified {simplify_count} tracked changes"

            if merge_runs:
                merge_count, merge_msg = do_merge_runs(str(output_path))
                if merge_msg.startswith("Error"):
                    return None, f"Error unpacking: {merge_msg}"
                message += f", merged {merge_count} runs"

        for xml_file in xml_files:
            _escape_smart_quotes(xml_file)

        return None, message

    except zipfile.BadZipFile:
        return None, f"Error: {input_file} is not a valid Office file"
    except Exception as e:
        return None, f"Error unpacking: {e}"


def _pretty_print_xml(xml_file: Path) -> None:
    """Parse xml_file through defusedxml (rejects DOCTYPEs/external entities/entity
    expansion) and rewrite it pretty-printed. Deliberately does NOT swallow parse
    failures — a genuine Office-generated XML part always parses cleanly, so a
    failure here (e.g. a DOCTYPE defusedxml refuses) means the input is suspect
    and the whole unpack should fail rather than silently leaving the original,
    unsanitized bytes on disk."""
    content = xml_file.read_text(encoding="utf-8")
    dom = defusedxml.minidom.parseString(content)
    xml_file.write_bytes(dom.toprettyxml(indent="  ", encoding="utf-8"))


def _escape_smart_quotes(xml_file: Path) -> None:
    try:
        content = xml_file.read_text(encoding="utf-8")
        for char, entity in SMART_QUOTE_REPLACEMENTS.items():
            content = content.replace(char, entity)
        xml_file.write_text(content, encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Unpack an Office file (DOCX, PPTX, XLSX) for editing"
    )
    parser.add_argument("input_file", help="Office file to unpack")
    parser.add_argument("output_directory", help="Output directory")
    parser.add_argument(
        "--merge-runs",
        type=lambda x: x.lower() == "true",
        default=True,
        metavar="true|false",
        help="Merge adjacent runs with identical formatting (DOCX only, default: true)",
    )
    parser.add_argument(
        "--simplify-redlines",
        type=lambda x: x.lower() == "true",
        default=True,
        metavar="true|false",
        help="Merge adjacent tracked changes from same author (DOCX only, default: true)",
    )
    args = parser.parse_args()

    _, message = unpack(
        args.input_file,
        args.output_directory,
        merge_runs=args.merge_runs,
        simplify_redlines=args.simplify_redlines,
    )
    print(message)

    if "Error" in message:
        sys.exit(1)
