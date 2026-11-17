"""
Công cụ Kiểm tra Kiến trúc 3 Lớp & Độ dài File (Architecture & Code Size Linter):
1. Kiểm tra nghiêm ngặt quy tắc phụ thuộc 1 chiều giữa 3 tầng:
   - Presentation (`presentation/`) KHÔNG ĐƯỢC import `infrastructure.*`, `psutil`, `subprocess`, `socket`, `winreg`, `utils.file_logger`.
   - Application (`application/`) KHÔNG ĐƯỢC import `presentation.*`, `PyQt6`, `psutil`, `socket`, `winreg`.
   - Infrastructure (`infrastructure/`) KHÔNG ĐƯỢC import `application.*`, `presentation.*`, `PyQt6`.
2. Thống kê số dòng code (LOC) của từng file và cảnh báo các file vượt ngưỡng khuyến nghị.

Sử dụng:
    python utils/arch_linter.py [--max-lines 500]
"""

import os
import sys
import ast
import argparse
from typing import Dict, List, Set, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LAYER_FORBIDDEN_IMPORTS: Dict[str, Tuple[str, ...]] = {
    "presentation": (
        "infrastructure",
        "psutil",
        "subprocess",
        "socket",
        "winreg",
        "utils.file_logger",
    ),
    "application": (
        "presentation",
        "PyQt6",
        "psutil",
        "socket",
        "winreg",
    ),
    "infrastructure": (
        "application",
        "presentation",
        "PyQt6",
    ),
}


def collect_imports_for_file(filepath: str) -> Set[str]:
    """Phân tích AST của một file Python và trả về tập hợp các module được import."""
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=filepath)
    imported: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod:
                imported.add(mod)
            for alias in node.names:
                imported.add(f"{mod}.{alias.name}" if mod else alias.name)
    return imported


def count_non_empty_lines(filepath: str) -> int:
    """Đếm số dòng code không trống của một file."""
    with open(filepath, "r", encoding="utf-8") as f:
        return sum(1 for ln in f if ln.strip())


def check_architecture_violations(project_root: str = PROJECT_ROOT) -> List[str]:
    """Quét toàn bộ 3 tầng và trả về danh sách các lỗi vi phạm phụ thuộc (nếu có)."""
    violations: List[str] = []
    for layer_dir, forbidden_prefixes in LAYER_FORBIDDEN_IMPORTS.items():
        abs_dir = os.path.join(project_root, layer_dir)
        if not os.path.isdir(abs_dir):
            continue
        for root, _dirs, files in os.walk(abs_dir):
            if "__pycache__" in root:
                continue
            for fname in sorted(files):
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, project_root)
                imports = collect_imports_for_file(fpath)
                for imp in sorted(imports):
                    for bad in forbidden_prefixes:
                        if imp == bad or imp.startswith(f"{bad}."):
                            violations.append(
                                f"[VI PHẠM TẦNG {layer_dir.upper()}] {rel_path} import '{imp}' (bị cấm: '{bad}')"
                            )
    return violations


def scan_file_lengths(
    project_root: str = PROJECT_ROOT, warn_threshold: int = 450
) -> Tuple[List[Tuple[str, int]], List[Tuple[str, int]]]:
    """
    Quét độ dài (số dòng không trống) của tất cả file .py trong 3 tầng.
    Trả về: (all_files_sorted_desc, warnings_over_threshold)
    """
    all_files: List[Tuple[str, int]] = []
    warnings: List[Tuple[str, int]] = []
    for layer_dir in ("presentation", "application", "infrastructure", "models", "utils"):
        abs_dir = os.path.join(project_root, layer_dir)
        if not os.path.isdir(abs_dir):
            continue
        for root, _dirs, files in os.walk(abs_dir):
            if "__pycache__" in root:
                continue
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, project_root)
                loc = count_non_empty_lines(fpath)
                all_files.append((rel_path, loc))
                if loc > warn_threshold:
                    warnings.append((rel_path, loc))
    all_files.sort(key=lambda x: x[1], reverse=True)
    warnings.sort(key=lambda x: x[1], reverse=True)
    return all_files, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Kiểm tra kiến trúc 3 lớp và độ dài file.")
    parser.add_argument("--warn-lines", type=int, default=450, help="Ngưỡng cảnh báo số dòng code (LOC)")
    args = parser.parse_args()

    violations = check_architecture_violations()
    all_files, warnings = scan_file_lengths(warn_threshold=args.warn_lines)

    print("=" * 72)
    print("  KIỂM TRA KIẾN TRÚC 3 LỚP (3-TIER ARCHITECTURE LINTER)")
    print("=" * 72)
    if violations:
        print(f"[FAILED] Phát hiện {len(violations)} vi phạm kiến trúc:")
        for v in violations:
            print(f"  - {v}")
        return 1
    else:
        print("[OK] 0 vi phạm phụ thuộc tầng (Presentation -> Application -> Infrastructure).")

    print("\n--- Thống kê Top 12 file theo số dòng code (LOC không trống) ---")
    for rel_path, loc in all_files[:12]:
        mark = " [WARN > ngưỡng]" if loc > args.warn_lines else ""
        print(f"  {loc:4d} LOC | {rel_path}{mark}")

    if warnings:
        print(f"\n[Notice] Có {len(warnings)} file vượt ngưỡng tham khảo {args.warn_lines} LOC.")
    else:
        print(f"\n[OK] Toàn bộ file đều nằm trong ngưỡng <= {args.warn_lines} LOC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
