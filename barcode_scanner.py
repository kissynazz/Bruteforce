#!/usr/bin/env python3
"""
Barcode Scanner — reads PDF417, Code 128, and Code 39 barcodes.

Usage:
  Scan all images in the mylicense/ directory:
    python barcode_scanner.py --mylicense

  Scan all images in any directory:
    python barcode_scanner.py --dir path/to/folder

  Scan specific image file(s):
    python barcode_scanner.py image.png
    python barcode_scanner.py image1.jpg image2.png

  Scan from webcam (live):
    python barcode_scanner.py --camera

  Save results to a file:
    python barcode_scanner.py --mylicense --output results.txt
    python barcode_scanner.py --camera --output results.txt

Dependencies:
  pip install -r requirements.txt
"""

import sys
import argparse
import datetime
from pathlib import Path

SUPPORTED_TYPES = {"PDF417", "CODE128", "CODE39"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp", ".gif"}
MYLICENSE_DIR = Path(__file__).parent / "mylicense"


def decode_image(image_path: str | Path) -> list[dict]:
    """Decode all supported barcodes from an image file."""
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode, ZBarSymbol
    except ImportError:
        print("ERROR: Missing dependencies. Run:  pip install -r requirements.txt")
        sys.exit(1)

    symbol_map = {
        "PDF417": ZBarSymbol.PDF417,
        "CODE128": ZBarSymbol.CODE128,
        "CODE39": ZBarSymbol.CODE39,
    }

    path = Path(image_path)
    if not path.exists():
        print(f"ERROR: File not found — {image_path}")
        return []

    img = Image.open(path)
    barcodes = decode(img, symbols=list(symbol_map.values()))

    results = []
    for barcode in barcodes:
        barcode_type = barcode.type
        if barcode_type in SUPPORTED_TYPES:
            results.append({
                "type": barcode_type,
                "data": barcode.data.decode("utf-8", errors="replace"),
                "rect": barcode.rect,
                "source": str(path),
            })
    return results


def scan_directory(directory: str | Path, output_file: str | None = None) -> None:
    """Scan all images in a directory for supported barcodes."""
    dir_path = Path(directory)

    if not dir_path.exists():
        print(f"ERROR: Directory not found — {dir_path}")
        sys.exit(1)

    if not dir_path.is_dir():
        print(f"ERROR: Not a directory — {dir_path}")
        sys.exit(1)

    images = sorted([
        f for f in dir_path.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
    ])

    if not images:
        print(f"No image files found in {dir_path}/")
        print(f"Supported formats: {', '.join(sorted(IMAGE_EXTENSIONS))}")
        return

    print(f"Found {len(images)} image(s) in {dir_path}/\n")

    total = 0
    out_lines: list[str] = []

    for img_path in images:
        print(f"Scanning: {img_path.name}")
        results = decode_image(img_path)

        if not results:
            print("  No supported barcodes found.\n")
        else:
            for r in results:
                print_result(r)
                line = f"{img_path.name}  |  {r['type']:<10}  |  {r['data']}"
                out_lines.append(line)
            total += len(results)

    print(f"Done — {total} barcode(s) found across {len(images)} file(s).")

    if output_file and out_lines:
        with open(output_file, "w") as f:
            f.write("\n".join(out_lines) + "\n")
        print(f"Results saved to {output_file}")


def scan_camera(output_file: str | None = None) -> None:
    """Scan barcodes live from the default webcam."""
    try:
        import cv2
        from pyzbar.pyzbar import decode, ZBarSymbol
    except ImportError:
        print("ERROR: Missing dependencies. Run:  pip install -r requirements.txt")
        sys.exit(1)

    symbol_map = {
        "PDF417": ZBarSymbol.PDF417,
        "CODE128": ZBarSymbol.CODE128,
        "CODE39": ZBarSymbol.CODE39,
    }

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("ERROR: Could not open webcam. Make sure a camera is connected.")
        sys.exit(1)

    print("Camera open — point at a barcode. Press Q to quit.\n")
    seen: set[str] = set()
    out_lines: list[str] = []

    while True:
        ret, frame = cap.read()
        if not ret:
            print("ERROR: Failed to grab frame.")
            break

        barcodes = decode(frame, symbols=list(symbol_map.values()))
        for barcode in barcodes:
            btype = barcode.type
            if btype not in SUPPORTED_TYPES:
                continue

            data = barcode.data.decode("utf-8", errors="replace")
            key = f"{btype}:{data}"

            pts = barcode.polygon
            if len(pts) == 4:
                import numpy as np
                hull = cv2.convexHull(
                    np.array([[p.x, p.y] for p in pts], dtype=np.int32)
                )
                cv2.polylines(frame, [hull], True, (0, 255, 0), 2)

            label = f"{btype}: {data}"
            x, y = barcode.rect.left, barcode.rect.top - 10
            cv2.putText(frame, label, (x, max(y, 20)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            if key not in seen:
                seen.add(key)
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                line = f"[{ts}] {btype:<10}  {data}"
                print(line)
                out_lines.append(line)

        cv2.imshow("Barcode Scanner — press Q to quit", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()

    if output_file and out_lines:
        with open(output_file, "w") as f:
            f.write("\n".join(out_lines) + "\n")
        print(f"\nResults saved to {output_file}")


def print_result(result: dict) -> None:
    print(f"  Type   : {result['type']}")
    print(f"  Data   : {result['data']}")
    print(f"  Region : {result['rect']}")
    print(f"  Source : {result['source']}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan PDF417, Code 128, and Code 39 barcodes from images or webcam.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python barcode_scanner.py --mylicense
  python barcode_scanner.py --mylicense --output results.txt
  python barcode_scanner.py --dir /path/to/images
  python barcode_scanner.py photo.png
  python barcode_scanner.py --camera
        """,
    )
    parser.add_argument(
        "images",
        nargs="*",
        help="Image file(s) to scan (.jpg, .png, .bmp, etc.)",
    )
    parser.add_argument(
        "--mylicense",
        action="store_true",
        help=f"Scan all images in the mylicense/ directory ({MYLICENSE_DIR})",
    )
    parser.add_argument(
        "--dir",
        metavar="DIRECTORY",
        help="Scan all images in the specified directory",
    )
    parser.add_argument(
        "--camera",
        action="store_true",
        help="Scan live from the default webcam instead of image files.",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Save scan results to a text file.",
    )
    args = parser.parse_args()

    if args.camera:
        scan_camera(output_file=args.output)
        return

    if args.mylicense:
        print(f"Scanning mylicense/ directory: {MYLICENSE_DIR}\n")
        scan_directory(MYLICENSE_DIR, output_file=args.output)
        return

    if args.dir:
        scan_directory(args.dir, output_file=args.output)
        return

    if args.images:
        total = 0
        for image_path in args.images:
            print(f"Scanning: {image_path}")
            results = decode_image(image_path)
            if not results:
                print("  No supported barcodes found.\n")
            else:
                for r in results:
                    print_result(r)
                total += len(results)
        print(f"Done — {total} barcode(s) found across {len(args.images)} file(s).")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
