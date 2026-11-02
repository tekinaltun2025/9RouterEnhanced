import os
import json
import re
import yaml
import shutil
import asyncio
import platform
import httpx
from pathlib import Path
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request
from pydantic import BaseModel
from typing import List, Dict, Any, Optional

app = FastAPI(title="9Router Uzak LLM Model Yönetim Aracı")

# Uygulama kök dizini (çapraz platform: dosya yolları app.py konumuna göre çözülür)
BASE_DIR = Path(__file__).resolve().parent

# Şablon dizini ayarı
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Konfigürasyon dosya yolu (env ile override edilebilir)
CONFIG_DIR = Path(os.environ.get("ROUTER_TOOL_CONFIG_DIR", str(BASE_DIR / "config")))
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = str(CONFIG_DIR / "9router_config.json")

# Varsayılan örnek konfigürasyon (Sadece uzak/bulut sağlayıcılar)
DEFAULT_CONFIG = {
    "providers": [
        {
            "name": "Groq Cloud",
            "endpoint": "https://api.groq.com/openai/v1",
            "free_models": ["llama3-8b-8192", "llama3-70b-8192", "mixtral-8x7b-32768"]
        },
        {
            "name": "Mistral AI",
            "endpoint": "https://api.mistral.ai/v1",
            "free_models": ["mistral-tiny", "mistral-small-latest"]
        },
        {
            "name": "Google Gemini Free API",
            "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "free_models": ["gemini-1.5-flash", "gemini-1.5-pro"]
        }
    ]
}

def init_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=4)

init_config()


async def probe_model(client: httpx.AsyncClient, endpoint: str, model: str) -> str:
    """
    Tek bir modelin erişilebilirliğini canlı olarak test eder.
    OpenAI uyumlu endpoint'lerde chat/completions denenir; Hugging Face inference endpoint'lerinde model varlığı kontrol edilir.
    Dönen değer: 'working' veya 'not-working'.
    """
    lower_ep = endpoint.lower()

    # Hugging Face inference endpoint'i için özel yol
    if "huggingface.co" in lower_ep and "/models" in lower_ep:
        try:
            r = await client.get(f"{endpoint.rstrip('/')}/{model}", timeout=8.0)
        except Exception:
            return "not-working"
        if r.status_code in (200, 401, 403, 429, 503):
            return "working"
        return "not-working"

    # OpenAI uyumlu endpoint'ler
    url = endpoint.rstrip('/') + "/chat/completions"
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    try:
        r = await client.post(url, json=payload, timeout=12.0)
    except Exception:
        return "not-working"
    code = r.status_code
    if code in (200, 401, 403, 429):
        return "working"
    if code == 404:
        text = r.text.lower()
        if "model" in text and any(k in text for k in ["not found", "not exist", "unknown", "invalid"]):
            return "not-working"
        return "working"
    if code == 400:
        text = r.text.lower()
        if "model" in text:
            return "not-working"
        return "working"
    if code >= 500:
        return "not-working"
    return "not-working"


def find_9router_db() -> str:
    """
    9Router SQLite veritabanının yolunu çapraz platform olarak bulur.
    Öncelik: NINEROUTER_DB env değişkeni -> bilinen OS'a özgü konumlar -> None.
    """
    env_path = os.environ.get("NINEROUTER_DB")
    if env_path and os.path.exists(env_path):
        return env_path

    home = Path.home()
    system = platform.system()
    candidates = []

    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            candidates.append(Path(appdata) / "9router" / "data" / "db" / "data.sqlite")
        if localappdata:
            candidates.append(Path(localappdata) / "9router" / "data" / "db" / "data.sqlite")
        candidates.append(home / ".9router" / "data" / "db" / "data.sqlite")
    elif system == "Darwin":
        candidates.append(home / "Library" / "Application Support" / "9router" / "data" / "db" / "data.sqlite")
        candidates.append(home / ".9router" / "data" / "db" / "data.sqlite")
    else:  # Linux ve diğer Unix
        candidates.append(home / ".9router" / "data" / "db" / "data.sqlite")
        candidates.append(home / ".config" / "9router" / "data" / "db" / "data.sqlite")

    for c in candidates:
        if c.exists():
            return str(c)
    return ""

# Sağlayıcı -> API anahtarı alma sayfası (kullanıcı kendi hesabından üretir)
PROVIDER_KEY_URLS = {
    "huggingface": "https://huggingface.co/settings/tokens",
    "openrouter": "https://openrouter.ai/keys",
    "deepinfra": "https://deepinfra.com/dashboard/account/api-keys",
    "sambanova": "https://cloud.sambanova.ai/app/settings/api-keys",
    "together": "https://api.together.xyz/settings/api-keys",
    "mistral": "https://console.mistral.ai/api-keys/",
    "groq": "https://console.groq.com/keys",
    "gemini": "https://aistudio.google.com/apikey",
    "google": "https://aistudio.google.com/apikey",
    "cloudflare": "https://dash.cloudflare.com/profile/api-tokens",
    "nous": "https://console.nousresearch.com/login",
    "nvidia": "https://build.nvidia.com",
    "ollama": "",
}

def key_url_for(provider):
    """Provider ad/endpoint'inden API anahtarı alma linkini bulur."""
    text = ((provider or {}).get("name", "") + " " + (provider or {}).get("endpoint", "")).lower()
    for kw, url in PROVIDER_KEY_URLS.items():
        if kw in text:
            return url
    return ""

def is_remote_endpoint(endpoint: str) -> bool:
    """
    Yerel ağ adreslerini (localhost, 127.0.0.1, 0.0.0.0, yerel IP'ler) kesinlikle filtreler.
    Sadece uzak/bulut tabanlı endpoint'lere izin verir.
    """
    if not endpoint:
        return False
    lower_ep = endpoint.lower()
    local_identifiers = ["localhost", "127.0.0.1", "0.0.0.0", "192.168.", "10.", "172.16."]
    for loc in local_identifiers:
        if loc in lower_ep:
            return False
    return True

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/models")
async def get_models():
    """
    9router üzerinde kayıtlı sağlayıcıları ve ücretsiz modelleri döner.
    Yerel adresler kesinlikle filtrelenir.
    """
    if not os.path.exists(CONFIG_FILE):
        return {"providers": []}
    
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    filtered_providers = []
    for prov in data.get("providers", []):
        endpoint = prov.get("endpoint", "")
        if is_remote_endpoint(endpoint):
            filtered_providers.append(prov)
            
    return {"providers": filtered_providers}

@app.post("/api/test-models")
async def test_models():
    """
    Tüm kayıtlı uzak sağlayıcıları ve modellerini canlı olarak, model bazında test eder.
    Her modelin test durumu Server-Sent Events (SSE) ile anlık olarak istemciye iletilir.
    'working' / 'not-working' durumları config dosyasına kaydedilir.
    """
    if not os.path.exists(CONFIG_FILE):
        raise HTTPException(status_code=404, detail="Konfigürasyon dosyası bulunamadı.")

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    async def event_stream():
        async with httpx.AsyncClient(timeout=15.0) as client:
            providers = data.get("providers", [])
            for p_index, prov in enumerate(providers):
                endpoint = prov.get("endpoint", "")
                models = prov.get("free_models", [])
                status_map = prov.get("model_status", {})
                if not is_remote_endpoint(endpoint):
                    for m_index, model in enumerate(models):
                        status_map[model] = "not-working"
                        yield f'data: {json.dumps({"type":"status","p_index":p_index,"m_index":m_index,"model":model,"status":"not-working"}, ensure_ascii=False)}\n\n'
                    prov["model_status"] = status_map
                    prov["status"] = "not-working"
                    yield f'data: {json.dumps({"type":"provider_done","p_index":p_index,"status":"not-working"}, ensure_ascii=False)}\n\n'
                    continue

                # Provider genel erişilebilirlik kontrolü (tek sefer)
                provider_reachable = False
                try:
                    r = await client.get(endpoint.rstrip('/') + "/models", timeout=6.0)
                    if r.status_code in (200, 401, 403, 429):
                        provider_reachable = True
                except Exception:
                    provider_reachable = any(k in endpoint.lower() for k in ["groq", "mistral", "googleapis", "huggingface", "openrouter", "cohere", "deepinfra", "together", "sambanova"])

                for m_index, model in enumerate(models):
                    # "Test ediliyor" durumunu anlık bildir
                    yield f'data: {json.dumps({"type":"status","p_index":p_index,"m_index":m_index,"model":model,"status":"testing"}, ensure_ascii=False)}\n\n'
                    await asyncio.sleep(0.05)

                    st = await probe_model(client, endpoint, model) if provider_reachable else "not-working"
                    status_map[model] = st
                    yield f'data: {json.dumps({"type":"status","p_index":p_index,"m_index":m_index,"model":model,"status":st}, ensure_ascii=False)}\n\n'

                prov["model_status"] = status_map
                prov["status"] = "working" if any(v == "working" for v in status_map.values()) else "not-working"
                yield f'data: {json.dumps({"type":"provider_done","p_index":p_index,"status":prov["status"]}, ensure_ascii=False)}\n\n'

        # Config dosyasına kaydet
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

        yield f'data: {json.dumps({"type":"done"}, ensure_ascii=False)}\n\n'

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.post("/api/upload")
async def upload_models():
    """
    Testten başarıyla geçen aktif modelleri gerçek 9router SQLite veritabanına 
    doğru şema (scope, key, value) ile yazar, böylece 9router içinde modeller görünür.
    """
    if not os.path.exists(CONFIG_FILE):
        raise HTTPException(status_code=404, detail="Konfigürasyon dosyası bulunamadı.")
        
    backup_path = CONFIG_FILE + ".bak"
    shutil.copy(CONFIG_FILE, backup_path)
    
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    # Sadece testten geçen çalışan modelleri ve provider'ları filtrele
    working_providers = []
    for prov in data.get("providers", []):
        m_status = prov.get("model_status", {})
        working_models = [m for m in prov.get("free_models", []) if m_status.get(m) == "working"]
        # Eğer hiç model test edilmediyse (ilk yükleme) tüm free_models kabul edilir, aksi takdirde sadece working olanlar
        if not m_status:
            working_models = prov.get("free_models", [])
            
        if working_models:
            prov["free_models"] = working_models
            working_providers.append(prov)
            
    data["providers"] = working_providers
    # Çalışmayan provider'ları config'den sil (kullanıcı isteği: Yükle diyince sadece çalışıyor olanlar eklensin)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        
    db_path = find_9router_db()
    if db_path and os.path.exists(db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        for prov in data.get("providers", []):
            prov_name = prov.get("name")
            prov_endpoint = prov.get("endpoint")
            alias = prov_name.lower().replace(" ", "-").replace("/", "-")
            # Gerçek API anahtarı (kullanıcı listeye eklerken girdi); yoksa yer tutucu
            real_key = (prov.get("api_key") or "").strip()
            key_value = real_key if real_key else "***"
            
            # 1. providerConnections tablosuna kaydet veya güncelle
            cursor.execute("""
                INSERT OR REPLACE INTO providerConnections (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """, (
                alias, 
                "openai-compatible", 
                "apikey", 
                prov_name, 
                None, 
                1, 
                1, 
                json.dumps({"apiKey": key_value, "endpoint": prov_endpoint, "testStatus": "active"})
            ))
            
            # 2. kv tablosuna (scope='customModels', key=model, value=json) modelleri ekle
            model_list = []
            for model in prov.get("free_models", []):
                key = f"{alias}|{model}|llm"
                model_list.append(model)
                # Önce var mı diye kontrol et (duplicate check)
                cursor.execute("SELECT value FROM kv WHERE scope='customModels' AND key=?", (key,))
                if cursor.fetchone() is None:
                    val = json.dumps({
                        "providerAlias": alias,
                        "id": model,
                        "type": "llm",
                        "name": model,
                        "caps": {"vision": False, "reasoning": False}
                    })
                    cursor.execute("""
                        INSERT INTO kv (scope, key, value)
                        VALUES ('customModels', ?, ?)
                    """, (key, val))
            
            # 3. Sağlayıcı bazlı model listesini global scope'a kaydet (9Router keşfi için)
            if model_list:
                cursor.execute("INSERT OR REPLACE INTO kv (scope, key, value) VALUES ('global', ?, ?)", 
                               (alias, json.dumps(model_list)))
                
        # 5. TumModellerSirayla ve AllmodelsTurn combosunu hem TR hem EN için kontrol et / oluştur / güncelle
        cursor.execute("SELECT key FROM kv WHERE scope='customModels'")
        custom_keys = [r[0] for r in cursor.fetchall()]
        all_custom_entries = []
        for k in custom_keys:
            parts = k.split('|')
            if len(parts) >= 2:
                entry = f"{parts[0]}/{parts[1]}"
                if entry not in all_custom_entries:
                    all_custom_entries.append(entry)

        for combo_name in ['TumModellerSirayla', 'AllmodelsTurn']:
            cursor.execute("SELECT models FROM combos WHERE name=?", (combo_name,))
            combo_row = cursor.fetchone()
            if combo_row:
                current_combo_models = json.loads(combo_row[0])
                new_entries = [e for e in all_custom_entries if e not in current_combo_models]
                if new_entries:
                    updated_combo = current_combo_models + new_entries
                    cursor.execute("UPDATE combos SET models = ?, updatedAt = datetime('now') WHERE name = ?", 
                                   (json.dumps(updated_combo), combo_name))
            else:
                # Eğer combo hiç yoksa oluştur
                cursor.execute("""
                    INSERT INTO combos (name, models, createdAt, updatedAt)
                    VALUES (?, ?, datetime('now'), datetime('now'))
                """, (combo_name, json.dumps(all_custom_entries)))

        conn.commit()
        conn.close()
        
    return {"status": "success", "message": "Aktif modeller 9router SQLite veritabanına (customModels scope) başarıyla ve mükerrer kayıt kontrolüyle işlendi!", "backup": backup_path}


@app.post("/api/remove-small-models")
async def remove_small_models(threshold: float = 7.0):
    """
    Kayıtlı uzak sağlayıcılardan ve 9Router veritabanından, kullanıcının belirlediği B değerinden
    (threshold) düşük parametreli modelleri kaldırır.
    """
    import re
    def is_small(model):
        m = (model or "").lower()
        match = re.search(r'(\d+(?:\.\d+)?)\s*b', m)
        if match:
            return float(match.group(1)) < threshold
        return False

    if not os.path.exists(CONFIG_FILE):
        raise HTTPException(status_code=404, detail="Konfigürasyon dosyası bulunamadı.")

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    removed = []
    for prov in data.get("providers", []):
        before = list(prov.get("free_models", []))
        after = [m for m in before if not is_small(m)]
        removed.extend([m for m in before if is_small(m)])
        prov["free_models"] = after
        ms = prov.get("model_status", {})
        for m in removed:
            ms.pop(m, None)
        prov["model_status"] = ms

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    # 9Router DB temizliği
    db_path = find_9router_db()
    if db_path and os.path.exists(db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT key FROM kv WHERE scope='customModels'")
        keys = [r[0] for r in cursor.fetchall()]
        for k in keys:
            parts = k.split('|')
            if len(parts) >= 2 and is_small(parts[1]):
                cursor.execute("DELETE FROM kv WHERE scope='customModels' AND key=?", (k,))
        cursor.execute("SELECT key, value FROM kv WHERE scope='global'")
        for key, val in cursor.fetchall():
            try:
                models = json.loads(val)
            except Exception:
                continue
            new_models = [m for m in models if not is_small((m.split('/')[-1] if '/' in m else m))]
            if new_models != models:
                cursor.execute("UPDATE kv SET value=? WHERE scope='global' AND key=?", (json.dumps(new_models), key))
        cursor.execute("SELECT name, models FROM combos")
        for name, val in cursor.fetchall():
            try:
                models = json.loads(val)
            except Exception:
                continue
            new_models = [m for m in models if not is_small((m.split('/')[-1] if '/' in m else m))]
            if new_models != models:
                cursor.execute("UPDATE combos SET models=?, updatedAt=datetime('now') WHERE name=?", (json.dumps(new_models), name))
        conn.commit()
        conn.close()

    return {"status": "success", "removed": removed, "count": len(removed)}

@app.post("/api/reset")
async def reset_config():
    """
    Yenile butonu: Ekrandaki filtreleri ve geçici test durumlarını sıfırlar, 
    son kaydedilen konfigürasyonu en güncel haliyle yeniden yükler (veri kaybı yaşatmaz).
    """
    if not os.path.exists(CONFIG_FILE):
        init_config()
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {"status": "success", "message": "Ekran ve liste en son kaydedilen güncel duruma sıfırlandı.", "providers": data.get("providers", [])}

@app.get("/api/scan-web")
async def scan_web():
    """
    Gelişmiş internet taraması: DuckDuckGo genel web araması + mnfst/0xzr ücretsiz LLM listeleri +
    bilinen ücretsiz sağlayıcıların model registry'leri + web'den çıkarılan aday endpoint'ler paralel
    taranır, doğrulanır ve listelenir. Yalnızca uzak (remote) adresler kabul edilir.
    """
    # Mevcut kaydedilmiş modeller (mükerrer eleme)
    existing_models = set()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cdata = json.load(f)
                for prov in cdata.get("providers", []):
                    for m in prov.get("free_models", []):
                        existing_models.add(m.lower())
        except Exception:
            pass

    def is_llm_model(mid):
        low = (mid or "").lower()
        if any(bad in low for bad in ["embedding", "embed", "tts", "whisper", "rerank", "bge", "reranker", "moderation", "guard"]):
            return False
        return True

    # --- 1) Genel internet araması (DuckDuckGo) ---
    search_text = ""
    search_urls = []
    try:
        def _ddg():
            from ddgs import DDGS
            out = []
            queries = [
                "free LLM API OpenAI compatible endpoint list",
                "free AI inference API providers",
                "free chat completion API",
                "open source LLM hosting free tier",
                "free LLM gateway API",
                "list of free LLM APIs for developers",
                "awesome free LLM APIs github",
            ]
            with DDGS() as ddgs:
                for q in queries:
                    try:
                        for r in ddgs.text(q, max_results=8):
                            out.append(r)
                    except Exception:
                        continue
            return out
        ddg_results = await asyncio.to_thread(_ddg)
        for r in ddg_results:
            search_text += " " + (r.get("title", "") + " " + r.get("body", "") + " " + r.get("href", ""))
            if r.get("href"):
                search_urls.append(r.get("href"))
    except Exception:
        pass

    # --- 2) Ücretsiz LLM liste sayfalarından aday endpoint URL'leri topla ---
    list_page_urls = [
        "https://0xzr.github.io/freellmpool/free-llm-api-providers-list.html",
        "https://raw.githubusercontent.com/mnfst/awesome-free-llm-apis/main/README.md",
    ]
    web_text = search_text
    web_candidate_urls = set(search_urls)
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            for u in list_page_urls:
                try:
                    r = await c.get(u)
                    if r.status_code == 200:
                        web_text += " " + r.text
                        for m in re.finditer(r"https?://[A-Za-z0-9.\-]+(?:/[A-Za-z0-9_\-./]*)?", r.text):
                            web_candidate_urls.add(m.group(0).rstrip(".,;:)"))
                except Exception:
                    continue
    except Exception:
        pass

    # Aday endpoint URL'leri: api içeren veya /v1 vb. ile biten
    web_candidates = set()
    for u in web_candidate_urls:
        low = u.lower()
        if any(tok in low for tok in ["/v1", "/v2", "/v4", "/v1beta", "/models", "/gateway", "/compatibility", "api."]):
            # docs/settings/keys sayfalarını ele
            if any(bad in low for bad in ["/docs/", "/terms", "/faq", "/guides", "/settings", "/keys", "apikey", "/account", "/app/api-keys", "github.com", "raw.githubusercontent"]):
                continue
            web_candidates.add(u.rstrip("/"))

    # Liste sayfalarından "Base URL:" / "OpenAI-compatible base URL:" desenlerini özellikle çıkar
    for m in re.finditer(r"(?:base\s*url|openai[- ]compatible\s*(?:base\s*)?url)[^`]*`([^`]+)`", web_text, re.IGNORECASE):
        u = m.group(1).strip().rstrip("/")
        if u.startswith("http") and is_remote_endpoint(u):
            web_candidates.add(u)

    # --- 3) Genişletilmiş ücretsiz sağlayıcı kataloğu ---
    curated = [
        {"name": "HuggingFace Serverless Inference API", "endpoint": "https://api-inference.huggingface.co/models", "models_url": "https://huggingface.co/api/models", "kind": "hf", "fallback": ["meta-llama/Meta-Llama-3-8B-Instruct", "Qwen/Qwen2.5-72B-Instruct", "mistralai/Mixtral-8x7B-Instruct-v0.1"], "keyUrl": "https://huggingface.co/settings/tokens"},
        {"name": "HuggingFace Router", "endpoint": "https://router.huggingface.co/v1", "models_url": "https://router.huggingface.co/v1/models", "kind": "generic", "fallback": ["meta-llama/Llama-3.1-8B-Instruct", "Qwen/Qwen2.5-7B-Instruct"], "keyUrl": "https://huggingface.co/settings/tokens"},
        {"name": "OpenRouter Free Tier Gateway", "endpoint": "https://openrouter.ai/api/v1", "models_url": "https://openrouter.ai/api/v1/models", "kind": "openrouter", "fallback": ["deepseek/deepseek-chat:free", "google/gemma-2-9b-it:free", "meta-llama/llama-3-8b-instruct:free"], "keyUrl": "https://openrouter.ai/keys"},
        {"name": "DeepInfra Serverless Free Endpoints", "endpoint": "https://api.deepinfra.com/v1/openai", "models_url": "https://api.deepinfra.com/v1/openai/models", "kind": "deepinfra", "fallback": ["meta-llama/Meta-Llama-3.1-8B-Instruct", "Qwen/Qwen2.5-Coder-32B-Instruct"], "keyUrl": "https://deepinfra.com/dashboard/account/api-keys"},
        {"name": "SambaNova Cloud Free Gateway", "endpoint": "https://api.sambanova.ai/v1", "models_url": "https://api.sambanova.ai/v1/models", "kind": "generic", "fallback": ["Meta-Llama-3.3-70B-Instruct", "DeepSeek-V3.2"], "keyUrl": "https://cloud.sambanova.ai/app/settings/api-keys"},
        {"name": "Groq Cloud", "endpoint": "https://api.groq.com/openai/v1", "models_url": "https://api.groq.com/openai/v1/models", "kind": "generic", "fallback": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768", "gemma2-9b-it"], "keyUrl": "https://console.groq.com/keys"},
        {"name": "Cerebras Inference", "endpoint": "https://api.cerebras.ai/v1", "models_url": "https://api.cerebras.ai/v1/models", "kind": "generic", "fallback": ["llama3.1-8b", "llama3.1-70b"], "keyUrl": "https://cloud.cerebras.ai/"},
        {"name": "Together AI", "endpoint": "https://api.together.xyz/v1", "models_url": "https://api.together.xyz/v1/models", "kind": "generic", "fallback": ["meta-llama/Llama-3-70b-chat-hf", "mistralai/Mixtral-8x7B-Instruct-v0.1"], "keyUrl": "https://api.together.xyz/settings/api-keys"},
        {"name": "Mistral AI", "endpoint": "https://api.mistral.ai/v1", "models_url": "https://api.mistral.ai/v1/models", "kind": "generic", "fallback": ["mistral-small-latest", "mistral-large-latest", "open-mistral-7b"], "keyUrl": "https://console.mistral.ai/api-keys/"},
        {"name": "Cohere", "endpoint": "https://api.cohere.ai/v1", "models_url": "https://api.cohere.ai/compatibility/v1/models", "kind": "generic", "fallback": ["command-r", "command-r-plus", "command-light"], "keyUrl": "https://dashboard.cohere.com/api-keys"},
        {"name": "Nebius AI Studio", "endpoint": "https://api.studio.nebius.ai/v1", "models_url": "https://api.studio.nebius.ai/v1/models", "kind": "generic", "fallback": ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct"], "keyUrl": "https://studio.nebius.ai/"},
        {"name": "Hyperbolic", "endpoint": "https://api.hyperbolic.xyz/v1", "models_url": "https://api.hyperbolic.xyz/v1/models", "kind": "generic", "fallback": ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct"], "keyUrl": "https://app.hyperbolic.xyz/settings"},
        {"name": "SiliconFlow", "endpoint": "https://api.siliconflow.cn/v1", "models_url": "https://api.siliconflow.cn/v1/models", "kind": "generic", "fallback": ["Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"], "keyUrl": "https://cloud.siliconflow.cn/account/ak"},
        {"name": "Fireworks AI", "endpoint": "https://api.fireworks.ai/inference/v1", "models_url": "https://api.fireworks.ai/inference/v1/models", "kind": "generic", "fallback": ["accounts/fireworks/models/llama-v3p1-70b-instruct", "accounts/fireworks/models/mixtral-8x7b-instruct"], "keyUrl": "https://fireworks.ai/account/api-keys"},
        {"name": "Novita AI", "endpoint": "https://api.novita.ai/v3/openai", "models_url": "https://api.novita.ai/v3/openai/models", "kind": "generic", "fallback": ["meta-llama/llama-3.1-70b-instruct", "deepseek/deepseek-r1"], "keyUrl": "https://novita.ai/settings/key-management"},
        {"name": "AI21 Studio", "endpoint": "https://api.ai21.com/studio/v1", "models_url": "https://api.ai21.com/studio/v1/models", "kind": "generic", "fallback": ["jamba-1.5-large", "jamba-1.5-mini"], "keyUrl": "https://studio.ai21.com/account/api-key"},
        {"name": "Replicate", "endpoint": "https://api.replicate.com/v1", "models_url": "https://api.replicate.com/v1/models", "kind": "generic", "fallback": ["meta/llama-2-70b-chat", "mistralai/mistral-7b-instruct-v0.2"], "keyUrl": "https://replicate.com/account/api-tokens"},
        {"name": "GitHub Models", "endpoint": "https://models.inference.ai.azure.com", "models_url": "https://models.inference.ai.azure.com/models", "kind": "generic", "fallback": ["gpt-4o-mini", "Meta-Llama-3.1-70B-Instruct"], "keyUrl": "https://github.com/settings/tokens"},
        {"name": "NVIDIA NIM", "endpoint": "https://integrate.api.nvidia.com/v1", "models_url": "https://integrate.api.nvidia.com/v1/models", "kind": "generic", "fallback": ["meta/llama-3.1-70b-instruct", "nvidia/nemotron-4-340b-instruct"], "keyUrl": "https://build.nvidia.com"},
        {"name": "Google Gemini", "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai", "models_url": "https://generativelanguage.googleapis.com/v1beta/openai/models", "kind": "generic", "fallback": ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"], "keyUrl": "https://aistudio.google.com/apikey"},
        {"name": "Cloudflare Workers AI", "endpoint": "https://api.cloudflare.com/client/v4/accounts", "models_url": None, "kind": "skip", "fallback": [], "keyUrl": "https://dash.cloudflare.com/profile/api-tokens"},
        {"name": "Z.ai / Zhipu GLM", "endpoint": "https://open.bigmodel.cn/api/paas/v4", "models_url": "https://open.bigmodel.cn/api/paas/v4/models", "kind": "generic", "fallback": ["glm-4-flash", "glm-4-air", "glm-4-plus"], "keyUrl": "https://open.bigmodel.cn/usercenter/apikeys"},
        {"name": "LLM7", "endpoint": "https://api.llm7.io/v1", "models_url": "https://api.llm7.io/v1/models", "kind": "generic", "fallback": ["gpt-4o-mini", "deepseek-chat"], "keyUrl": "https://llm7.io/"},
        {"name": "Kilo Gateway", "endpoint": "https://api.kilo.ai/api/gateway", "models_url": "https://api.kilo.ai/api/gateway/models", "kind": "generic", "fallback": ["kilo-auto/free"], "keyUrl": "https://kilo.ai/"},
        {"name": "ModelScope", "endpoint": "https://api-inference.modelscope.cn/v1", "models_url": "https://api-inference.modelscope.cn/v1/models", "kind": "generic", "fallback": ["Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3"], "keyUrl": "https://modelscope.cn/my/myaccesstoken"},
        {"name": "Aion Labs", "endpoint": "https://api.aionlabs.ai/v1", "models_url": "https://api.aionlabs.ai/v1/models", "kind": "generic", "fallback": ["aion-labs/aion-3.0"], "keyUrl": "https://www.aionlabs.ai/app/api-keys/"},
        {"name": "OVHcloud AI Endpoints", "endpoint": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", "models_url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/models", "kind": "generic", "fallback": ["Meta-Llama-3.1-8B-Instruct"], "keyUrl": "https://www.ovhcloud.com/"},
        {"name": "GonkaBroker", "endpoint": "https://proxy.gonkabroker.com/v1", "models_url": "https://proxy.gonkabroker.com/v1/models", "kind": "generic", "fallback": ["deepseek-ai/DeepSeek-V4-Flash"], "keyUrl": "https://gonkabroker.com/"},
        {"name": "Pollinations", "endpoint": "https://text.pollinations.ai/openai", "models_url": "https://text.pollinations.ai/models", "kind": "generic", "fallback": ["openai"], "keyUrl": "https://pollinations.ai/"},
        {"name": "Vercel AI Gateway", "endpoint": "https://ai-gateway.vercel.sh/v1", "models_url": "https://ai-gateway.vercel.sh/v1/models", "kind": "generic", "fallback": ["openai/gpt-4o-mini"], "keyUrl": "https://vercel.com/"},
        {"name": "Requesty", "endpoint": "https://router.requesty.ai/v1", "models_url": "https://router.requesty.ai/v1/models", "kind": "generic", "fallback": ["openai/gpt-4o-mini"], "keyUrl": "https://requesty.ai/"},
        {"name": "Ollama Cloud", "endpoint": "https://ollama.com/api", "models_url": "https://ollama.com/api/tags", "kind": "generic", "fallback": ["llama3.1:8b"], "keyUrl": "https://ollama.com/"},
    ]

    def extract_models(data):
        models = []
        if isinstance(data, dict):
            arr = data.get("data") or data.get("models") or data.get("results") or data.get("tags") or []
        elif isinstance(data, list):
            arr = data
        else:
            arr = []
        for m in arr:
            if isinstance(m, str):
                models.append(m)
            elif isinstance(m, dict):
                mid = m.get("id") or m.get("name") or m.get("model") or m.get("slug") or m.get("digest")
                if mid:
                    models.append(mid)
        return models

    async def fetch_curated(client, prov):
        kind = prov["kind"]
        if kind == "skip" or not prov.get("models_url"):
            return prov["fallback"] or []
        if kind == "hf":
            try:
                r = await client.get(prov["models_url"], params={"pipeline_tag": "text-generation", "sort": "downloads", "direction": "-1", "limit": "20", "inference": "warm"}, timeout=8.0)
                if r.status_code == 200:
                    models = [m.get("id") for m in r.json() if m.get("id")]
                    if models:
                        return [m for m in models[:12] if is_llm_model(m)]
            except Exception:
                pass
            return [m for m in (prov["fallback"] or []) if is_llm_model(m)]
        if kind == "openrouter":
            try:
                r = await client.get(prov["models_url"], timeout=8.0)
                if r.status_code == 200:
                    models = []
                    for m in r.json().get("data", []):
                        if m.get("pricing", {}).get("prompt") == "0" and m.get("pricing", {}).get("completion") == "0":
                            models.append(m["id"])
                    if models:
                        return [m for m in models[:12] if is_llm_model(m)]
            except Exception:
                pass
            return [m for m in (prov["fallback"] or []) if is_llm_model(m)]
        if kind == "deepinfra":
            try:
                r = await client.get(prov["models_url"], timeout=8.0)
                if r.status_code == 200:
                    models = []
                    for m in r.json().get("data", []):
                        mid = m.get("id", "")
                        if any(k in mid.lower() for k in ["llama", "qwen", "mistral", "gemma", "deepseek", "phi", "gpt-oss", "coder"]):
                            if is_llm_model(mid):
                                models.append(mid)
                    if models:
                        return models[:12]
            except Exception:
                pass
            return [m for m in (prov["fallback"] or []) if is_llm_model(m)]
        # generic
        try:
            r = await client.get(prov["models_url"], timeout=8.0)
            if r.status_code == 200:
                models = extract_models(r.json())
                models = [m for m in models if is_llm_model(m)]
                if models:
                    return models[:12]
        except Exception:
            pass
        return [m for m in (prov["fallback"] or []) if is_llm_model(m)]

    scanned_results = []
    async with httpx.AsyncClient(timeout=12.0) as client:
        # Curated providers
        for prov in curated:
            if prov["kind"] == "skip":
                continue
            models = await fetch_curated(client, prov)
            filtered = [m for m in models if m.lower() not in existing_models]
            if filtered:
                scanned_results.append({
                    "name": prov["name"],
                    "endpoint": prov["endpoint"],
                    "free_models": filtered,
                    "source": "curated",
                })

        # Web-discovered candidate endpoints (probe)
        sem = asyncio.Semaphore(8)
        async def probe_candidate(url):
            async with sem:
                # Olası API yolları
                candidates = [url]
                if not url.lower().endswith("/models"):
                    candidates.append(url.rstrip("/") + "/models")
                if not url.lower().endswith("/v1") and "/v1" not in url.lower():
                    candidates.append(url.rstrip("/") + "/v1/models")
                for v in candidates:
                    if not is_remote_endpoint(v):
                        continue
                    try:
                        r = await client.get(v, timeout=6.0)
                        if r.status_code == 200:
                            models = extract_models(r.json())
                            models = [m for m in models if is_llm_model(m)]
                            if models:
                                return v, models[:12]
                    except Exception:
                        continue
                return None, None

        probe_tasks = [probe_candidate(u) for u in list(web_candidates)[:60]]
        probe_results = await asyncio.gather(*probe_tasks, return_exceptions=True)
        seen_endpoints = {s["endpoint"].lower() for s in scanned_results}
        for res in probe_results:
            if isinstance(res, Exception) or not res:
                continue
            url, models = res
            if not url or not models:
                continue
            base = url
            if base.lower().endswith("/models"):
                base = base[:-len("/models")]
            if base.lower() in seen_endpoints:
                continue
            if not is_remote_endpoint(base):
                continue
            filtered = [m for m in models if m.lower() not in existing_models]
            if not filtered:
                continue
            # Daha iyi bir ad: hostname'den türet
            try:
                from urllib.parse import urlparse
                host = urlparse(base).netloc or base
                name = "Web-Discovered: " + host
            except Exception:
                name = "Web-Discovered Endpoint"
            scanned_results.append({
                "name": name,
                "endpoint": base,
                "free_models": filtered,
                "source": "web",
            })
            seen_endpoints.add(base.lower())

    # --- 4) finalize ---
    valid_results = []
    seen = set()
    for res in scanned_results:
        if not is_remote_endpoint(res["endpoint"]):
            continue
        if res["endpoint"].lower() in seen:
            continue
        seen.add(res["endpoint"].lower())
        res["keyUrl"] = key_url_for(res)
        valid_results.append(res)

    return {"status": "success", "providers": valid_results}

class AddProviderRequest(BaseModel):
    name: str
    endpoint: str
    free_models: List[str]
    api_key: str = ""

@app.post("/api/add-provider")
async def add_provider(item: AddProviderRequest):
    """
    Sekme 2'de bulunan sağlayıcıyı Sekme 1'deki ana test ve yükleme havuzuna kalıcı olarak ekler.
    """
    if not is_remote_endpoint(item.endpoint):
        raise HTTPException(status_code=400, detail="Güvenlik Kuralı: Yerel ağ adresleri eklenemez.")
        
    if not os.path.exists(CONFIG_FILE):
        init_config()

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    providers = data.get("providers", [])
    existing = False
    for prov in providers:
        if prov["name"].lower() == item.name.lower() or prov["endpoint"] == item.endpoint:
            prov["free_models"] = list(set(prov["free_models"] + item.free_models))
            existing = True
            break
            
    if not existing:
        providers.append({
            "name": item.name,
            "endpoint": item.endpoint,
            "free_models": item.free_models,
            "api_key": item.api_key or ""
        })
        
    data["providers"] = providers
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        
    return {"status": "success", "message": f"'{item.name}' başarıyla ana model havuzuna kaydedildi.", "providers": providers}

if __name__ == "__main__":
    import uvicorn
    import os
    host = os.environ.get("ROUTER_TOOL_HOST", "127.0.0.1")
    port = int(os.environ.get("ROUTER_TOOL_PORT", "8099"))
    uvicorn.run(app, host=host, port=port)
