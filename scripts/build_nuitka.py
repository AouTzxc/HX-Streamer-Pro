import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOGO_ICO = ROOT / "logo.ico"
RELEASE_VERSION = "1.0.0"

APPS = {
    "sender": {
        "entry": ROOT / "main.py",
        "binary_name": "HX_Streamer_Pro",
        "product_name": "HX Streamer Pro",
    },
    "receiver": {
        "entry": ROOT / "receiver.py",
        "binary_name": "HX_Receiver_Pro",
        "product_name": "HX Streamer Receiver",
    },
}


def detect_platform_name():
    value = platform.system().lower()
    if value.startswith("win"):
        return "windows"
    if value.startswith("darwin"):
        return "macos"
    if value.startswith("linux"):
        return "linux"
    raise RuntimeError(f"Unsupported platform: {platform.system()}")


def has_non_ascii(value):
    return any(ord(ch) > 127 for ch in str(value))


def ensure_supported_build_path(platform_name):
    if platform_name != "windows":
        return
    if has_non_ascii(ROOT):
        raise RuntimeError(
            "Nuitka Windows onefile build may fail in non-ASCII paths. "
            f"Current path: {ROOT}\n"
            "Move the project to an ASCII path (e.g. C:\\src\\HX-Streamer-Pro) "
            "or run the GitHub Actions workflow for official artifacts."
        )


def run_cmd(cmd):
    print(" ".join(str(part) for part in cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def ensure_zstandard_for_onefile(mode):
    if mode != "onefile":
        return
    try:
        __import__("zstandard")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "zstandard is required for compressed onefile builds. "
            "Run: uv sync --group build"
        ) from exc


def build_one(app_key, output_root):
    app = APPS[app_key]
    platform_name = args.platform
    mode = args.mode
    if mode == "auto":
        if platform_name == "windows":
            mode = "onefile"
        elif platform_name == "macos":
            mode = "app"
        else:
            mode = "standalone"

    ensure_zstandard_for_onefile(mode)

    app_output_dir = output_root / f"{platform_name}-{args.macos_arch if platform_name == 'macos' else 'native'}" / app_key
    app_output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        f"--mode={mode}",
        "--enable-plugins=pyqt6",
        "--assume-yes-for-downloads",
        "--remove-output",
        f"--include-data-files={LOGO_ICO}=logo.ico",
        f"--output-dir={app_output_dir}",
        f"--output-filename={app['binary_name']}",
        f"--product-name={app['product_name']}",
        "--company-name=HX Streamer Team",
        f"--product-version={RELEASE_VERSION}",
        f"--file-version={RELEASE_VERSION}.0",
        "--copyright=GPL-3.0-only",
        str(app["entry"]),
    ]

    if platform_name == "windows":
        cmd.insert(-1, "--windows-console-mode=disable")
        if LOGO_ICO.exists():
            cmd.insert(-1, f"--windows-icon-from-ico={LOGO_ICO}")

    if platform_name == "macos":
        cmd.insert(-1, "--macos-app-mode=gui")
        cmd.insert(-1, f"--macos-target-arch={args.macos_arch}")
        cmd.insert(-1, f"--macos-app-name={app['product_name']}")
        if args.create_dmg:
            cmd.insert(-1, "--macos-app-create-dmg")

    run_cmd(cmd)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build sender/receiver with Nuitka."
    )
    parser.add_argument(
        "--platform",
        choices=["auto", "windows", "macos", "linux"],
        default="auto",
        help="Target platform mode selection.",
    )
    parser.add_argument(
        "--app",
        choices=["all", "sender", "receiver"],
        default="all",
        help="Which app to build.",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "onefile", "standalone", "app", "app-dist"],
        default="auto",
        help="Nuitka mode. auto picks onefile on Windows, app on macOS.",
    )
    parser.add_argument(
        "--macos-arch",
        choices=["native", "x86_64", "arm64", "universal"],
        default="native",
        help="Only used on macOS builds.",
    )
    parser.add_argument(
        "--create-dmg",
        action="store_true",
        help="Create DMG for macOS app builds.",
    )
    parser.add_argument(
        "--output-root",
        default="build/nuitka",
        help="Output root directory.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete output-root before build.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.platform == "auto":
        args.platform = detect_platform_name()

    ensure_supported_build_path(args.platform)

    output_root = ROOT / args.output_root
    if args.clean and output_root.exists():
        shutil.rmtree(output_root)

    targets = ["sender", "receiver"] if args.app == "all" else [args.app]
    for app_key in targets:
        build_one(app_key, output_root)
