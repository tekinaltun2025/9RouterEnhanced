#!/usr/bin/env python3
"""
Çapraz platform başlatıcı: 9Router Enhanced (router_tool) FastAPI uygulamasını çalıştırır.
Windows, Linux ve macOS'ta çalışır.
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

def main():
    try:
        import uvicorn
    except ImportError:
        print("uvicorn bulunamadı. Lütfen 'pip install -r requirements.txt' çalıştırın.")
        sys.exit(1)

    host = os.environ.get("ROUTER_TOOL_HOST", "127.0.0.1")
    port = int(os.environ.get("ROUTER_TOOL_PORT", "8099"))
    reload = os.environ.get("ROUTER_TOOL_RELOAD", "0") == "1"

    print(f"9Router Enhanced başlatılıyor: http://{host}:{port}")
    uvicorn.run(
        "app:app",
        host=host,
        port=port,
        reload=reload,
        app_dir=str(BASE_DIR),
    )

if __name__ == "__main__":
    main()
