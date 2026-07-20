#!/usr/bin/env python3
"""
Traverse AMI metadata schema version 2.0, extract source.object.type and
source.object.format values from digitized_*.json schemas, and output an
Excel workbook with consolidated mappings and lists.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Set
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter


def extract_field_values(data: Any, target_path: List[str]) -> Set[str]:
    """
    Recursively search a JSON schema dictionary for a specific logical path 
    (e.g., ['source', 'object', 'type']) handling both JSON Schema 
    'properties' nesting and direct dict structures.
    """
    results = set()

    if not isinstance(data, dict):
        return results

    # 1. Check for flattened dot-notation key (e.g., "source.object.type")
    dot_key = ".".join(target_path)
    if dot_key in data:
        val = data[dot_key]
        results.update(_parse_value(val))

    # 2. Check for exact current key in path
    current_key = target_path[0]
    remaining_path = target_path[1:]

    # Check direct child or JSON Schema 'properties' child
    next_nodes = []
    if current_key in data:
        next_nodes.append(data[current_key])
    if "properties" in data and isinstance(data["properties"], dict) and current_key in data["properties"]:
        next_nodes.append(data["properties"][current_key])
    if "fields" in data and isinstance(data["fields"], dict) and current_key in data["fields"]:
        next_nodes.append(data["fields"][current_key])

    for node in next_nodes:
        if not remaining_path:
            # We reached the target field! Extract its values.
            results.update(_parse_value(node))
        else:
            # Continue traversing down the path
            results.update(extract_field_values(node, remaining_path))

    # 3. Fallback: recursively search all dict values in case the root is wrapped
    for key, value in data.items():
        if key not in ("properties", "fields", current_key) and isinstance(value, dict):
            results.update(extract_field_values(value, target_path))

    return results


def _parse_value(node: Any) -> Set[str]:
    """Extract actual string values from a JSON schema property definition."""
    vals = set()
    if isinstance(node, dict):
        if "enum" in node and isinstance(node["enum"], list):
            vals.update(str(x) for x in node["enum"] if x is not None)
        elif "const" in node and node["const"] is not None:
            vals.add(str(node["const"]))
        elif "default" in node and node["default"] is not None:
            vals.add(str(node["default"]))
        elif "value" in node and node["value"] is not None:
            vals.add(str(node["value"]))
    elif isinstance(node, (str, int, float)):
        vals.add(str(node))
    elif isinstance(node, list):
        vals.update(str(x) for x in node if x is not None)
    return vals


def format_excel_sheet(worksheet):
    """Apply clean styling and auto-fit column widths to an openpyxl worksheet."""
    header_fill = PatternFill(start_color="2B5B84", end_color="2B5B84", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    
    # Style header row
    for col in range(1, worksheet.max_column + 1):
        cell = worksheet.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="left", vertical="center")
    
    worksheet.row_dimensions[1].height = 24

    # Auto-fit columns
    for col in worksheet.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.value is not None:
                max_len = max(max_len, len(str(cell.value)))
        worksheet.column_dimensions[col_letter].width = max(max_len + 3, 12)


def main():
    parser = argparse.ArgumentParser(
        description="Extract source.object.type and source.object.format from AMI schema version 2.0 into an Excel workbook."
    )
    parser.add_argument(
        "-i", "--input",
        required=True,
        type=Path,
        help="Path to the root ami-metadata directory"
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        type=Path,
        help="Path for the output .xlsx file (e.g., ami_metadata_2.0_summary.xlsx)"
    )
    parser.add_argument(
        "-v", "--version",
        default="2.0",
        help="Target schema version folder to process (default: 2.0)"
    )

    args = parser.parse_args()

    if not args.input.exists() or not args.input.is_dir():
        parser.error(f"Input directory does not exist or is not a directory: {args.input}")

    # Target specifically the requested version directory (defaults to 2.0)
    schema_dir = args.input / "versions" / args.version / "schema"
    if not schema_dir.exists():
        parser.error(f"Could not find schema directory for version '{args.version}' at: {schema_dir}")

    raw_records = []
    all_types = set()
    all_formats = set()
    
    # Mapping structure: { type_name: set(format_names) }
    type_format_map: Dict[str, Set[str]] = {}

    print(f"Scanning schema version {args.version} at: {schema_dir}...")

    # Target schema files starting with "digitized_"
    for schema_file in sorted(schema_dir.glob("digitized_*.json")):
        try:
            with open(schema_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[WARNING] Failed to read or parse {schema_file}: {e}")
            continue

        types = extract_field_values(data, ["source", "object", "type"])
        formats = extract_field_values(data, ["source", "object", "format"])

        # Fallback if empty: label as Unspecified to maintain visibility
        if not types:
            types = {"[Not Found]"}
        if not formats:
            formats = {"[Not Found]"}

        all_types.update(t for t in types if t != "[Not Found]")
        all_formats.update(f for f in formats if f != "[Not Found]")

        for t in sorted(types):
            for fmt in sorted(formats):
                raw_records.append({
                    "Schema File": schema_file.name,
                    "source.object.type": t,
                    "source.object.format": fmt
                })
                
                if t != "[Not Found]" and fmt != "[Not Found]":
                    if t not in type_format_map:
                        type_format_map[t] = set()
                    type_format_map[t].add(fmt)

    if not raw_records:
        print(f"No matching schema files or data found in version {args.version}. Exiting.")
        return

    print(f"Processed {len(raw_records)} records from version {args.version}. Generating Excel workbook...")

    # 1. Prepare DataFrames
    df_raw = pd.DataFrame(raw_records)

    # 2. Unique Lists DataFrame (padded to equal length for clean column display)
    sorted_types = sorted(list(all_types))
    sorted_formats = sorted(list(all_formats))
    max_len = max(len(sorted_types), len(sorted_formats))
    
    df_unique = pd.DataFrame({
        f"Version {args.version} Unique Types": sorted_types + [""] * (max_len - len(sorted_types)),
        f"Version {args.version} Unique Formats": sorted_formats + [""] * (max_len - len(sorted_formats))
    })

    # 3. Type to Allowable Formats Mapping DataFrame
    mapping_records = []
    for t in sorted(type_format_map.keys()):
        for fmt in sorted(type_format_map[t]):
            mapping_records.append({
                "source.object.type": t,
                "Allowable Format": fmt
            })
    df_mapping = pd.DataFrame(mapping_records)

    # Write to Excel with styling
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        df_mapping.to_excel(writer, sheet_name="Type-Format Mapping", index=False)
        df_unique.to_excel(writer, sheet_name="Unique Lists", index=False)
        df_raw.to_excel(writer, sheet_name="Schema Data Audit", index=False)

        # Apply formatting to all tabs
        for sheetname in writer.sheets:
            format_excel_sheet(writer.sheets[sheetname])

    print(f"Success! Excel report generated at: {args.output.resolve()}")


if __name__ == "__main__":
    main()