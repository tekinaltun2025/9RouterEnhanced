#!/usr/bin/env python3
"""
Çapraz platform masaüstü kısayolu kurulumu.
Linux: ~/.local/share/applications ve ~/Desktop altına .desktop dosyası.
Windows: Start Menu (ve isteğe bağlı Desktop) altına .lnk kısayolu.
macOS: ~/Applications altına .app benzeri basit bir kısayol (osascript ile).
"""
import os
import sys
import platform
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
APP_NAME = "9Router Enhanced"
LAUNCHER = BASE_DIR / "launcher.py"
ICON_PNG = BASE_DIR / "icon.png"


def install_linux():
    desktop_entry = f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Comment=Uzak LLM sağlayıcı yönetimi ve test aracı
Exec=python3 {LAUNCHER}
Path={BASE_DIR}
Icon={ICON_PNG if ICON_PNG.exists() else 'applications-internet'}
Terminal=false
Categories=Network;Utility;
"""
    targets = [
        Path.home() / ".local" / "share" / "applications" / "router-tool.desktop",
        Path.home() / "Desktop" / "router-tool.desktop",
    ]
    for t in targets:
        if t.exists():
            print(f"Zaten mevcut, atlanıyor: {t}")
            continue
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(desktop_entry, encoding="utf-8")
        try:
            t.chmod(0o755)
        except Exception:
            pass
        print(f"Oluşturuldu: {t}")
    # icon hicolor (varsa)
    print("Linux masaüstü kısayolları kuruldu.")


def install_windows():
    # PowerShell ile WScript.Shell kullanarak kısayol oluştur
    start_menu = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    start_menu.mkdir(parents=True, exist_ok=True)
    shortcut_path = start_menu / "9Router Enhanced.lnk"
    desktop = Path.home() / "Desktop"
    desktop_shortcut = desktop / "9Router Enhanced.lnk"

    ps_script = f'''
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("{shortcut_path}")
$s.TargetPath = "python"
$s.Arguments = '"{LAUNCHER}"'
$s.WorkingDirectory = "{BASE_DIR}"
$s.Description = "9Router Enhanced - Uzak LLM yönetim aracı"
$s.Save()
'''
    subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=False)
    print(f"Oluşturuldu: {shortcut_path}")
    # Desktop kısayolu da oluştur
    ps_script2 = f'''
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("{desktop_shortcut}")
$s.TargetPath = "python"
$s.Arguments = '"{LAUNCHER}"'
$s.WorkingDirectory = "{BASE_DIR}"
$s.Description = "9Router Enhanced - Uzak LLM yönetim aracı"
$s.Save()
'''
    try:
        desktop.mkdir(parents=True, exist_ok=True)
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_script2], check=False)
        print(f"Oluşturuldu: {desktop_shortcut}")
    except Exception as e:
        print(f"Desktop kısayolu oluşturulamadı: {e}")
    print("Windows masaüstü kısayolları kuruldu.")


def install_macos():
    # Basit bir .command dosyası (çift tıkla çalıştır)
    cmd = Path.home() / "Desktop" / "9Router Enhanced.command"
    cmd.write_text(f'#!/bin/bash\ncd "{BASE_DIR}"\npython3 launcher.py\n', encoding="utf-8")
    try:
        cmd.chmod(0o755)
    except Exception:
        pass
    print(f"Oluşturuldu: {cmd}")
    print("macOS kısayolu kuruldu.")


def main():
    system = platform.system()
    if system == "Linux":
        install_linux()
    elif system == "Windows":
        install_windows()
    elif system == "Darwin":
        install_macos()
    else:
        print(f"Desteklenmeyen işletim sistemi: {system}")
        sys.exit(1)


if __name__ == "__main__":
    main()
