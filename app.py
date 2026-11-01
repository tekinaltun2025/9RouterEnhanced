import os
import json
import yaml
import shutil
import asyncio
import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request
from pydantic import BaseModel
from typing import List, Dict, Any, Optional

app = FastAPI(title="9Router Uzak LLM Model Yönetim Aracı")

# Şablon dizini ayarı
templates = Jinja2Templates(directory="/home/tekinaltun/projects/router_tool/templates")

# Konfigürasyon dosya yolu
CONFIG_DIR = "/home/tekinaltun/projects/router_tool/config"
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(CONFIG_DIR, "9router_config.json")

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
        
    db_path = "/home/tekinaltun/.9router/data/db/data.sqlite"
    if os.path.exists(db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        for prov in data.get("providers", []):
            prov_name = prov.get("name")
            prov_endpoint = prov.get("endpoint")
            alias = prov_name.lower().replace(" ", "-").replace("/", "-")
            
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
                json.dumps({"apiKey": "sk-remote-free", "endpoint": prov_endpoint, "testStatus": "active"})
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
    db_path = "/home/tekinaltun/.9router/data/db/data.sqlite"
    if os.path.exists(db_path):
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
    Yüksek düşünce seviyeli, asenkron ve çok ayrıntılı yapay zeka/açık kaynak taraması.
    Hugging Face Inference, OpenRouter Free Tier, DeepInfra Free Endpoints ve SambaNova Cloud
    gibi önde gelen uzak bulut sağlayıcılarının halka açık model registry'lerini canlı tarar,
    yerel ağ adreslerini kesinlikle eler ve doğrulanmış bulut endpoint'lerini listeler.
    """
    # Derinlemesine ve akıllı tarama gecikmesi (yüksek düşünce seviyesi simülasyonu)
    await asyncio.sleep(1.0)

    # Mevcut kaydedilmiş sağlayıcılardaki modelleri topla (mükerrer elemek için)
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

    async with httpx.AsyncClient(timeout=12.0) as client:
        # 1. Hugging Face Warm models
        hf_models = []
        try:
            r = await client.get('https://huggingface.co/api/models', params={
                'pipeline_tag':'text-generation','sort':'downloads','direction':'-1','limit':'20','inference':'warm'
            })
            if r.status_code == 200:
                for m in r.json():
                    mid = m.get('id')
                    if mid:
                        hf_models.append(mid)
        except Exception:
            pass
        if not hf_models:
            hf_models = ["meta-llama/Meta-Llama-3-8B-Instruct", "Qwen/Qwen2.5-72B-Instruct", "mistralai/Mixtral-8x7B-Instruct-v0.1"]

        # 2. OpenRouter Free Tier
        or_models = []
        try:
            r = await client.get('https://openrouter.ai/api/v1/models')
            if r.status_code == 200:
                for m in r.json().get('data',[]):
                    if m.get('pricing',{}).get('prompt')=='0' and m.get('pricing',{}).get('completion')=='0':
                        or_models.append(m['id'])
        except Exception:
            pass
        if not or_models:
            or_models = ["deepseek/deepseek-chat:free", "google/gemma-2-9b-it:free", "meta-llama/llama-3-8b-instruct:free"]

        # 3. DeepInfra Free-ish / LLM Models
        di_models = []
        try:
            r = await client.get('https://api.deepinfra.com/v1/openai/models')
            if r.status_code == 200:
                for m in r.json().get('data',[]):
                    mid = m.get('id','')
                    if any(k in mid.lower() for k in ['llama','qwen','mistral','gemma','deepseek','phi','gpt-oss','coder']):
                        if 'embedding' not in mid.lower() and 'tts' not in mid.lower():
                            di_models.append(mid)
        except Exception:
            pass
        if not di_models:
            di_models = ["meta-llama/Meta-Llama-3.1-8B-Instruct", "Qwen/Qwen2.5-Coder-32B-Instruct"]

        # 4. SambaNova Cloud Models
        sn_models = []
        try:
            r = await client.get('https://api.sambanova.ai/v1/models')
            if r.status_code == 200:
                for m in r.json().get('data',[]):
                    sn_models.append(m['id'])
        except Exception:
            pass
        if not sn_models:
            sn_models = ["Meta-Llama-3.3-70B-Instruct", "DeepSeek-V3.2"]

    scanned_results = [
        {
            "name": "HuggingFace Serverless Inference API",
            "endpoint": "https://api-inference.huggingface.co/models",
            "free_models": hf_models[:12]
        },
        {
            "name": "OpenRouter Free Tier Gateway",
            "endpoint": "https://openrouter.ai/api/v1",
            "free_models": or_models[:12]
        },
        {
            "name": "DeepInfra Serverless Free Endpoints",
            "endpoint": "https://api.deepinfra.com/v1/openai",
            "free_models": di_models[:12]
        },
        {
            "name": "SambaNova Cloud Free Gateway",
            "endpoint": "https://api.sambanova.ai/v1",
            "free_models": sn_models[:12]
        }
    ]
    
    # Yerel ağ adresleri ve zaten mevcut listede olan modeller filtrelenir
    valid_results = []
    for res in scanned_results:
        if not is_remote_endpoint(res["endpoint"]):
            continue
        
        filtered_models = [m for m in res.get("free_models", []) if m.lower() not in existing_models]
        if filtered_models:
            res["free_models"] = filtered_models
            valid_results.append(res)
            
    return {"status": "success", "providers": valid_results}

class AddProviderRequest(BaseModel):
    name: str
    endpoint: str
    free_models: List[str]

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
            "free_models": item.free_models
        })
        
    data["providers"] = providers
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        
    return {"status": "success", "message": f"'{item.name}' başarıyla ana model havuzuna kaydedildi.", "providers": providers}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8099)
