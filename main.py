from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, PlainTextResponse, Response
from pydantic import BaseModel
from pymongo import MongoClient
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from datetime import datetime
import os, logging, csv, io, re, base64, time, unicodedata
from typing import List, Optional
from PIL import Image
from fastapi.templating import Jinja2Templates
import google.generativeai as genai

# Configurar logging para ver errores en los logs de Render
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

templates = Jinja2Templates(directory="templates")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Conexión MongoDB (Render usará la variable de entorno MONGO_URL)
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
logger.info(f"Conectando a MongoDB: {MONGO_URL[:30]}...")

try:
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    # Forzar conexión para detectar errores temprano
    client.server_info()
    db = client["capsule_cloud_db"]
    collection = db["stores"]
    MONGO_OK = True
    logger.info("✅ MongoDB conectado exitosamente.")
except Exception as e:
    logger.error(f"❌ Error conectando a MongoDB: {e}")
    MONGO_OK = False
    collection = None

# Configurar Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("✅ Gemini API configurada.")
else:
    logger.warning("⚠️ GEMINI_API_KEY no encontrada.")

class AppData(BaseModel):
    store_id: str
    inventory: list = []
    salesHistory: list = []
    usersDB: list = []
    customersDB: list = []
    settings: dict = {}

def clean_mongo_doc(doc):
    """Elimina el campo _id de MongoDB para evitar errores de serialización JSON."""
    if doc and "_id" in doc:
        del doc["_id"]
    return doc

@app.get("/api/data/{store_id}")
def get_data(store_id: str):
    if not MONGO_OK or collection is None:
        logger.warning("MongoDB no disponible. Retornando error para activar modo offline.")
        raise HTTPException(status_code=503, detail="MongoDB no conectado")
    try:
        data = collection.find_one({"_id": store_id})
        if not data:
            logger.info(f"Tienda '{store_id}' no encontrada, retornando estructura inicial.")
            return {
                "inventory": [],
                "salesHistory": [],
                "usersDB": [{"user": "admin", "pass": "admin123", "role": "admin"}],
                "customersDB": [],
                "settings": {"name": store_id}
            }
        # CRÍTICO: Eliminar _id para evitar error de serialización ObjectId → JSON
        clean_mongo_doc(data)
        logger.info(f"Tienda '{store_id}' cargada: {len(data.get('inventory', []))} productos.")
        return data
    except Exception as e:
        logger.error(f"Error leyendo tienda '{store_id}': {e}")
        return {
            "inventory": [],
            "salesHistory": [],
            "usersDB": [{"user": "admin", "pass": "admin123", "role": "admin"}],
            "customersDB": [],
            "settings": {"name": store_id}
        }

@app.post("/api/data")
def save_data(data: AppData):
    if not MONGO_OK or collection is None:
        logger.error("MongoDB no disponible. No se pudo guardar.")
        raise HTTPException(status_code=503, detail="MongoDB no conectado")
    try:
        store_data = data.dict()
        store_id = store_data.pop("store_id")
        
        if "inventory" in store_data:
            import re
            for p in store_data["inventory"]:
                desc = p.get("desc", "")
                box = p.get("box", "")
                if not box and desc:
                    match = re.search(r'caja\s*[:#\-]?\s*([a-zA-Z0-9\-\#]+)', str(desc), re.IGNORECASE)
                    if match:
                        p["box"] = match.group(1).upper()
                        
        collection.update_one({"_id": store_id}, {"$set": store_data}, upsert=True)
        logger.info(f"✅ Tienda '{store_id}' guardada: {len(store_data.get('inventory', []))} productos.")
        return {"status": "success", "store": store_id}
    except Exception as e:
        logger.error(f"Error guardando tienda: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Endpoint super rápido para UptimeRobot (evita spin down en Render)
@app.get("/ping")
@app.head("/ping")
def ping():
    return {"ping": "pong"}

# Endpoint de diagnóstico para verificar estado de MongoDB
@app.get("/api/health")
def health_check():
    if MONGO_OK:
        try:
            client.server_info()
            count = collection.count_documents({})
            return {"status": "ok", "mongo": "connected", "stores_count": count}
        except Exception as e:
            return {"status": "degraded", "mongo": "error", "detail": str(e)}
    return {"status": "error", "mongo": "disconnected"}

# ─────────────────────────────────────────────────────────────
# ALMACENAMIENTO DE IMÁGENES Y MULTIMEDIA (BACKBLAZE B2 + CDN)
# ─────────────────────────────────────────────────────────────
B2_KEY_ID = os.getenv("B2_KEY_ID", "00571e14fccc7420000000001")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY", "K005GdLSkK2H5CvFoXnvKzr27EYXC9c")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME", "capsule-imagenes")
CDN_DOMAIN = os.getenv("CDN_DOMAIN", "img.capsuleshop.net")

_b2_auth_cache = {
    "auth_token": None,
    "api_url": None,
    "bucket_id": None,
    "expires_at": 0
}

def get_b2_credentials():
    """Obtiene credenciales de B2 (prioriza variables de entorno y luego settings en MongoDB)"""
    key_id = os.getenv("B2_KEY_ID", B2_KEY_ID)
    app_key = os.getenv("B2_APPLICATION_KEY", B2_APPLICATION_KEY)
    bucket_name = os.getenv("B2_BUCKET_NAME", B2_BUCKET_NAME)
    cdn_domain = os.getenv("CDN_DOMAIN", CDN_DOMAIN)
    
    if MONGO_OK and collection is not None:
        try:
            store = collection.find_one({"_id": "main_store"})
            if store and "settings" in store:
                s = store["settings"]
                if s.get("b2KeyId"): key_id = s.get("b2KeyId")
                if s.get("b2AppKey"): app_key = s.get("b2AppKey")
                if s.get("b2BucketName"): bucket_name = s.get("b2BucketName")
                if s.get("cdnDomain"): cdn_domain = s.get("cdnDomain")
        except Exception:
            pass
            
    return key_id, app_key, bucket_name, cdn_domain

def authorize_b2(force_refresh: bool = False):
    """Autentica con la API de Backblaze B2 y almacena el token en caché"""
    global _b2_auth_cache
    now = time.time()
    if not force_refresh and _b2_auth_cache["auth_token"] and _b2_auth_cache["expires_at"] > now:
        return _b2_auth_cache["api_url"], _b2_auth_cache["auth_token"], _b2_auth_cache["bucket_id"]
        
    key_id, app_key, bucket_name, _ = get_b2_credentials()
    if not key_id or not app_key:
        raise HTTPException(status_code=500, detail="Credenciales de Backblaze B2 no configuradas")
        
    auth_string = f"{key_id}:{app_key}"
    auth_base64 = base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")
    
    resp = requests.get(
        "https://api.backblazeb2.com/b2api/v2/b2_authorize_account",
        headers={"Authorization": f"Basic {auth_base64}"},
        timeout=15
    )
    
    if resp.status_code != 200:
        logger.error(f"Error autorizando B2: {resp.text}")
        raise HTTPException(status_code=502, detail=f"Error conectando a Backblaze B2: {resp.text}")
        
    data = resp.json()
    bucket_id = data.get("allowed", {}).get("bucketId")
    
    if not bucket_id:
        api_url = data["apiUrl"]
        auth_token = data["authorizationToken"]
        account_id = data["accountId"]
        list_resp = requests.post(
            f"{api_url}/b2api/v2/b2_list_buckets",
            headers={"Authorization": auth_token},
            json={"accountId": account_id, "bucketName": bucket_name},
            timeout=15
        )
        if list_resp.status_code == 200:
            buckets = list_resp.json().get("buckets", [])
            for b in buckets:
                if b.get("bucketName") == bucket_name:
                    bucket_id = b.get("bucketId")
                    break
                    
    _b2_auth_cache = {
        "api_url": data["apiUrl"],
        "auth_token": data["authorizationToken"],
        "bucket_id": bucket_id,
        "expires_at": now + 3600 * 20
    }
    return _b2_auth_cache["api_url"], _b2_auth_cache["auth_token"], _b2_auth_cache["bucket_id"]

def clean_filename(name: str) -> str:
    accents = {'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u', 'ñ': 'n',
               'Á': 'A', 'É': 'E', 'Í': 'I', 'Ó': 'O', 'Ú': 'U', 'Ñ': 'N'}
    for k, v in accents.items():
        name = name.replace(k, v)
    name = unicodedata.normalize('NFKD', name).encode('ASCII', 'ignore').decode('utf-8')
    name = re.sub(r'[^a-zA-Z0-9_\.]', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    return name

def compress_and_optimize_image(file_bytes: bytes, filename: str, max_width: int = 1200, quality: int = 82):
    """Comprime y optimiza una imagen para web usando Pillow"""
    try:
        img = Image.open(io.BytesIO(file_bytes))
        if getattr(img, "is_animated", False):
            return file_bytes, "image/gif", filename
            
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[3] if len(img.split()) > 3 else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
            
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.Resampling.LANCZOS)
            
        out_buf = io.BytesIO()
        img.save(out_buf, format='JPEG', quality=quality, optimize=True)
        out_buf.seek(0)
        
        base_name = os.path.splitext(filename)[0]
        new_filename = f"{base_name}.jpg"
        return out_buf.getvalue(), "image/jpeg", new_filename
    except Exception as e:
        logger.warning(f"No se pudo comprimir con PIL, usando archivo original: {e}")
        return file_bytes, "application/octet-stream", filename

def upload_bytes_to_b2(file_bytes: bytes, file_name: str, content_type: str = "image/jpeg", folder: str = "products"):
    """Sube un buffer a Backblaze B2 y genera la URL pública del CDN"""
    api_url, auth_token, bucket_id = authorize_b2()
    
    url_get_upload = f"{api_url}/b2api/v2/b2_get_upload_url"
    resp_upload_url = requests.post(
        url_get_upload,
        headers={"Authorization": auth_token},
        json={"bucketId": bucket_id},
        timeout=15
    )
    
    if resp_upload_url.status_code != 200:
        logger.warning("Upload URL falló. Forzando refresh de autenticación B2...")
        api_url, auth_token, bucket_id = authorize_b2(force_refresh=True)
        resp_upload_url = requests.post(
            f"{api_url}/b2api/v2/b2_get_upload_url",
            headers={"Authorization": auth_token},
            json={"bucketId": bucket_id},
            timeout=15
        )
        if resp_upload_url.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Error obteniendo URL de subida B2: {resp_upload_url.text}")
            
    upload_data = resp_upload_url.json()
    upload_url = upload_data["uploadUrl"]
    upload_auth_token = upload_data["authorizationToken"]
    
    clean_name = clean_filename(file_name)
    if not clean_name:
        clean_name = f"upload_{int(time.time())}.jpg"
        
    b2_file_path = f"{folder.strip('/')}/{clean_name}" if folder else clean_name
    
    headers_upload = {
        "Authorization": upload_auth_token,
        "X-Bz-File-Name": b2_file_path,
        "Content-Type": content_type,
        "X-Bz-Content-Sha1": "do_not_verify"
    }
    
    resp_upload = requests.post(upload_url, headers=headers_upload, data=file_bytes, timeout=45)
    if resp_upload.status_code != 200:
        logger.error(f"Error subiendo archivo a B2: {resp_upload.text}")
        raise HTTPException(status_code=502, detail=f"Error al subir a Backblaze B2: {resp_upload.text}")
        
    _, _, bucket_name, cdn_domain = get_b2_credentials()
    
    if cdn_domain:
        clean_domain = cdn_domain.replace('https://', '').replace('http://', '').strip('/')
        public_url = f"https://{clean_domain}/file/{bucket_name}/{b2_file_path}"
    else:
        public_url = f"https://f005.backblazeb2.com/file/{bucket_name}/{b2_file_path}"
        
    return public_url, b2_file_path

@app.post("/api/upload")
async def upload_file(
    file: Optional[UploadFile] = File(None),
    files: Optional[List[UploadFile]] = File(None),
    folder: str = Form("products")
):
    """Endpoint unificado para subir 1 o múltiples imágenes/videos a Backblaze B2"""
    upload_list = []
    if files:
        upload_list.extend(files)
    if file and file not in upload_list:
        upload_list.append(file)
        
    if not upload_list:
        raise HTTPException(status_code=400, detail="No se enviaron archivos para subir")
        
    results = []
    for f in upload_list:
        content = await f.read()
        filename = f.filename or f"upload_{int(time.time())}.jpg"
        mime = f.content_type or "image/jpeg"
        
        # Optimizar si es imagen
        if mime.startswith("image/"):
            optimized_bytes, final_mime, final_filename = compress_and_optimize_image(content, filename)
        else:
            optimized_bytes = content
            final_mime = mime
            final_filename = filename
            
        public_url, b2_path = upload_bytes_to_b2(optimized_bytes, final_filename, final_mime, folder=folder)
        results.append({
            "url": public_url,
            "path": b2_path,
            "original_filename": filename,
            "size": len(optimized_bytes)
        })
        
    return {
        "status": "success",
        "url": results[0]["url"] if results else "",
        "urls": [r["url"] for r in results],
        "files": results
    }

@app.get("/api/storage/status")
def get_storage_status():
    """Diagnóstico de la conexión con Backblaze B2 y CDN"""
    key_id, _, bucket_name, cdn_domain = get_b2_credentials()
    try:
        api_url, auth_token, bucket_id = authorize_b2(force_refresh=True)
        return {
            "status": "connected",
            "provider": "Backblaze B2",
            "bucket_name": bucket_name,
            "bucket_id": bucket_id,
            "cdn_domain": cdn_domain,
            "key_id_prefix": key_id[:6] + "..." if key_id else "none",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error comprobando estado de B2: {e}")
        return {
            "status": "error",
            "provider": "Backblaze B2",
            "bucket_name": bucket_name,
            "cdn_domain": cdn_domain,
            "detail": str(e)
        }

# Endpoints de SEO (Robots y Sitemap)
@app.get("/robots.txt", response_class=PlainTextResponse)
def get_robots_txt():
    content = "User-agent: *\nAllow: /\nSitemap: https://capsuleshop.net/sitemap.xml\n"
    return content

@app.get("/sitemap.xml")
def get_sitemap():
    base_url = "https://capsuleshop.net"
    store_id = "main_store"
    
    urls = []
    urls.append(f"{base_url}/")
    
    if MONGO_OK and collection is not None:
        store = collection.find_one({"_id": store_id})
        if store:
            for p in store.get("inventory", []):
                if isinstance(p, dict) and p.get("isPublic", True) is not False:
                    try:
                        if int(p.get("stock", 0)) > 0:
                            urls.append(f"{base_url}/p/{p.get('id')}")
                    except (ValueError, TypeError):
                        pass
    
    xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml_content += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for url in urls:
        xml_content += f"  <url>\n    <loc>{url}</loc>\n    <changefreq>daily</changefreq>\n  </url>\n"
    xml_content += '</urlset>'
    
    return Response(content=xml_content, media_type="application/xml")

# Servir Frontend Administrativo (Capsule OS)
@app.get("/admin")
def serve_admin():
    return FileResponse("static/index.html")

# Catálogo Público (SSR para SEO)
@app.get("/")
@app.get("/p/{product_id}")
@app.get("/catalog/{store_id}")
@app.get("/catalog/{store_id}/p/{product_id}")
def serve_public_catalog(request: Request, store_id: str = "main_store", product_id: str = None):
    store = {}
    if MONGO_OK and collection is not None:
        store = collection.find_one({"_id": store_id}) or {}
        clean_mongo_doc(store)
        
    settings = store.get("settings") or {"name": "Catálogo", "about": "Explora nuestra tienda"}
    raw_inventory = store.get("inventory") or []
    
    inventory = []
    for p in raw_inventory:
        if isinstance(p, dict) and p.get("isPublic", True) is not False:
            try:
                if int(p.get("stock", 0)) > 0:
                    inventory.append(p)
            except (ValueError, TypeError):
                pass
    
    product = next((p for p in inventory if str(p.get("id")) == str(product_id)), None) if product_id else None
    
    return templates.TemplateResponse(
        request=request,
        name="catalog.html",
        context={
            "request": request, 
            "store_id": store_id, 
            "settings": settings,
            "inventory": inventory,
            "product": product,
            "base_url": "https://capsuleshop.net"
        }
    )

@app.get("/catalog")
def serve_catalog_default(request: Request):
    return serve_public_catalog(request, "main_store")

class ChatRequest(BaseModel):
    message: str
    history: list = []

@app.post("/api/chat/{store_id}")
def chat_with_bot(store_id: str, req: ChatRequest):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Gemini API no configurada")
        
    store = {}
    if MONGO_OK and collection is not None:
        store = collection.find_one({"_id": store_id}) or {}
        
    settings = store.get("settings", {})
    store_name = settings.get("name", "Capsule Store")
    currency = settings.get("currency", "HNL")
    
    raw_inventory = store.get("inventory", [])
    inventory = []
    for p in raw_inventory:
        if isinstance(p, dict) and p.get("isPublic", True) is not False:
            try:
                if int(p.get("stock", 0)) > 0:
                    prod_url = f"/catalog/{store_id}/p/{p.get('id')}"
                    cat_url = f"/catalog/{store_id}?cat={p.get('category').replace(' ', '%20')}" if p.get('category') else f"/catalog/{store_id}"
                    inventory.append(f"- {p.get('name')} (Categoría: {p.get('category')}): {currency} {p.get('price')} | Stock: {p.get('stock')} | Link Producto: {prod_url} | Link Categoría: {cat_url}")
            except (ValueError, TypeError):
                pass
                
    inventory_str = "\\n".join(inventory) if inventory else "No hay productos disponibles por ahora."
    
    system_instruction = f"""
    Eres un asistente virtual amable y servicial para la tienda '{store_name}'.
    Tu objetivo es ayudar a los clientes a encontrar productos, responder dudas sobre métodos de pago, envío, y disponibilidad.
    Si el cliente desea hablar con un humano o asesor, indícale amablemente que puede hacer clic en el botón de WhatsApp que está en la interfaz.
    
    Aquí está nuestro inventario actual:
    {inventory_str}
    
    REGLAS IMPORTANTES:
    1. Responde de forma corta, clara, y amigable, con emojis.
    2. Cuando el cliente pregunte por algo, haz un resumen general y luego dale una lista corta (máximo 4-5 opciones principales) con su precio.
    3. SIEMPRE debes incluir un enlace clicable al producto usando el formato Markdown estricto: [Ver producto](/ruta-del-producto).
    4. También puedes invitarlo a ver toda la categoría usando el formato: [Ver todos los productos de esta categoría](/ruta-de-categoria).
    5. No muestres las URLs directamente, siempre ponlas dentro de un texto clicable con [texto](url).
    """
    
    try:
        model = genai.GenerativeModel(
            model_name='gemini-2.5-flash',
            system_instruction=system_instruction
        )
        
        # Convertir historial al formato de Gemini
        formatted_history = []
        for msg in req.history:
            role = "user" if msg["role"] == "user" else "model"
            formatted_history.append({"role": role, "parts": [msg["content"]]})
            
        chat = model.start_chat(history=formatted_history)
        response = chat.send_message(req.message)
        
        return {"response": response.text}
    except Exception as e:
        logger.error(f"Error en chat: {e}")
        raise HTTPException(status_code=500, detail="Error procesando tu mensaje")

# Facebook Data Feed (CSV)
@app.get("/api/feed/{store_id}.csv")
def get_facebook_feed(store_id: str):
    if not MONGO_OK or collection is None:
        raise HTTPException(status_code=503, detail="MongoDB no conectado")
        
    store = collection.find_one({"_id": store_id})
    if not store:
        raise HTTPException(status_code=404, detail="Tienda no encontrada")
        
    inventory = store.get("inventory", [])
    settings = store.get("settings", {})
    
    # Facebook requiere código de divisa ISO (ej. HNL, USD)
    raw_currency = settings.get("currency", "HNL").strip().upper()
    if raw_currency in ["L", "L.", "LPS", "LEMPIRAS"]:
        currency_iso = "HNL"
    elif raw_currency == "$":
        currency_iso = "USD"
    elif len(raw_currency) == 3:
        currency_iso = raw_currency
    else:
        currency_iso = "HNL"  # Default seguro para este usuario
        
    base_url = "https://capsuleshop.net"
    
    # Mapeo exacto basado en el documento de Word
    category_mapping = {
        # Tecnología gadgets
        "Adaptadores Ac": "Tecnología gadgets",
        "Adaptadores Y Cables": "Tecnología gadgets",
        "Apple Accesorios": "Tecnología gadgets",
        "Audio Accesorios": "Tecnología gadgets",
        "Auriculares": "Tecnología gadgets",
        "Auto Accesorios": "Tecnología gadgets",
        "Bluetooth": "Tecnología gadgets",
        "Cables Y Adaptadores": "Tecnología gadgets",
        "Cables Y Cargadores": "Tecnología gadgets",
        "Cargadores Laptop": "Tecnología gadgets",
        "Computación Accesorios": "Tecnología gadgets",
        "Fotografía Accesorios": "Tecnología gadgets",
        "Smart Y Accesorios": "Tecnología gadgets",
        "Soportes Y Bases": "Tecnología gadgets",
        "Video Accesorios": "Tecnología gadgets",
        "Videojuegos Y Accesorios": "Tecnología gadgets",
        "Internet Accesorios": "Tecnología gadgets",
        "Hubs Y Lectores": "Tecnología gadgets",
        "Digitales": "Tecnología gadgets",
        
        # Coleccionismo vintage
        "Vintage": "Coleccionismo vintage",
        "Coleccionables": "Coleccionismo vintage",
        "Liquidación Combos": "Coleccionismo vintage",
        
        # Hogar y salud
        "Bolsos": "Hogar y salud",
        "Covers Y Fundas": "Hogar y salud",
        "Deporte Sport": "Hogar y salud",
        "Herramientas": "Hogar y salud",
        "Hogar": "Hogar y salud",
        "Salud Y Belleza": "Hogar y salud",
        "Mascotas": "Hogar y salud",
        "Música E Instrumentos": "Hogar y salud"
    }

    def classify_product(subcategoria):
        # Limpieza por si hay espacios
        sub_clean = str(subcategoria).strip()
        # Buscar en el mapa, si no está, mandarlo a Tecnología por defecto o Sin categoría
        return category_mapping.get(sub_clean, "Tecnología gadgets")

    output = io.StringIO()
    writer = csv.writer(output)
    
    # Cabeceras requeridas por Facebook (agregado additional_image_link, google_product_category, custom_label_0)
    writer.writerow(["id", "title", "description", "availability", "condition", "price", "link", "image_link", "additional_image_link", "brand", "google_product_category", "custom_label_0"])
    
    for p in inventory:
        if p.get("isPublic") is False:
            continue
            
        try:
            stock = int(p.get("stock", 0))
        except (ValueError, TypeError):
            stock = 0
            
        # Excluir productos sin stock del catálogo de Facebook
        if stock <= 0:
            continue
            
        pid = p.get("id", "")
        title = p.get("name", "")
        desc = p.get("desc", "") or title
        subcategoria_db = p.get("category", "")
        
        availability = "in stock"
        condition = "new"
        
        try:
            price_val = float(p.get("price", 0))
        except (ValueError, TypeError):
            price_val = 0.0
            
        price = f"{price_val:.2f} {currency_iso}"
        link = f"{base_url}/catalog/{store_id}#producto-{pid}"
        
        img1 = p.get("img1", "")
        img2 = p.get("img2", "")
        img3 = p.get("img3", "")
        
        # Usar img1 como principal, si está vacía usar otra
        main_img = img1 or img2 or img3
        additional_imgs = [img for img in [img2, img3, img1] if img and img != main_img]
        additional_image_link = ",".join(additional_imgs[:10])  # CSV separador por comas
        
        brand = settings.get("name", "Capsule Store")
        
        category = classify_product(subcategoria_db)
        
        writer.writerow([pid, title, desc, availability, condition, price, link, main_img, additional_image_link, brand, category, category])
        
    output.seek(0)
    return StreamingResponse(output, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=feed_{store_id}.csv"})

app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Automatización del Informe Diario ──
def generate_and_send_daily_report():
    if not MONGO_OK or collection is None:
        logger.warning("MongoDB no disponible. No se puede generar informe.")
        return
        
    google_script_url = os.getenv("GOOGLE_SCRIPT_URL")
    emails_str = os.getenv("REPORT_EMAILS")
    
    if not all([google_script_url, emails_str]):
        logger.warning("Falta GOOGLE_SCRIPT_URL o REPORT_EMAILS. Informe omitido.")
        return

    try:
        now = datetime.now()
        
        store = collection.find_one({"_id": "main_store"})
        if not store or "salesHistory" not in store:
            logger.info("No se encontró historial de ventas para generar informe.")
            return
            
        sales = store.get("salesHistory", [])
        
        today_sales = []
        for s in sales:
            if s.get("status") == "ANULADA":
                continue
                
            d_str = s.get("date", "")
            if d_str.endswith("Z"):
                d_str = d_str[:-1] + "+00:00"
                
            try:
                dt_utc = datetime.fromisoformat(d_str)
                dt_local = dt_utc.astimezone()
                if dt_local.date() == now.date():
                    today_sales.append(s)
            except Exception:
                pass
                
        total_vendido = sum(s.get("total", 0) for s in today_sales)
        total_ganancia = sum(s.get("profit", 0) for s in today_sales)
        
        prod_counter = {}
        for sale in today_sales:
            for item in sale.get("items", []):
                pid = item.get("id")
                if pid not in prod_counter:
                    prod_counter[pid] = {"name": item.get("name"), "qty": 0, "revenue": 0}
                prod_counter[pid]["qty"] += item.get("qty", 0)
                prod_counter[pid]["revenue"] += item.get("price", 0) * item.get("qty", 0)
                
        all_prods = sorted(prod_counter.values(), key=lambda x: x["qty"], reverse=True)
        
        date_str = now.strftime("%Y-%m-%d")
        curr = store.get("settings", {}).get("currency", "L")
        store_name = store.get("settings", {}).get("name", "Capsule Shop")
        
        html = f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
        <style>
            body{{font-family:Arial,sans-serif;color:#1f2937;padding:20px;max-width:800px;margin:auto;}}
            h1{{color:#4f46e5;border-bottom:3px solid #4f46e5;padding-bottom:8px;}}
            h2{{color:#374151;margin-top:20px;border-bottom:1px solid #e5e7eb;font-size:14px;text-transform:uppercase;}}
            .kpi-grid{{display:table;width:100%;margin:16px 0;}}
            .kpi{{display:table-cell;background:#f9fafb;border-left:4px solid #4f46e5;padding:12px;border-radius:6px;}}
            .kpi.green{{border-left-color:#10b981;}}
            table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:12px;text-align:left;}}
            th{{background:#4f46e5;color:#fff;padding:8px;}}
            td{{padding:7px;border-bottom:1px solid #f3f4f6;}}
        </style></head><body>
        <h1>📊 Informe Diario — {store_name}</h1>
        <p>Generado: {now.strftime('%d/%m/%Y, %I:%M %p')} | Transacciones Hoy: {len(today_sales)}</p>
        <div class="kpi-grid">
            <div class="kpi"><h3>Vendido: {curr} {total_vendido:,.2f}</h3></div>
            <div class="kpi green"><h3>Ganancia: {curr} {total_ganancia:,.2f}</h3></div>
        </div>
        <h2>Productos Vendidos Hoy</h2>
        <table><thead><tr><th>Producto</th><th>Cant.</th><th>Ingresos</th></tr></thead><tbody>
        """
        for p in all_prods:
            html += f"<tr><td>{p['name']}</td><td>{p['qty']}</td><td>{curr} {p['revenue']:,.2f}</td></tr>"
            
        html += """</tbody></table>
        <h2>Detalle de Ventas</h2>
        <table><thead><tr><th>Factura</th><th>Cliente</th><th>Total</th></tr></thead><tbody>
        """
        for s in today_sales:
            html += f"<tr><td>{s.get('id')}</td><td>{s.get('customer')}</td><td>{curr} {s.get('total'):,.2f}</td></tr>"
            
        html += f"""</tbody></table>
        <p style="margin-top:30px;font-size:10px;color:#9ca3af;text-align:center;">Generado automáticamente por Capsule OS</p>
        </body></html>"""
        
        payload = {
            "to": emails_str,
            "subject": f"📊 Informe Diario de Ventas - {date_str}",
            "html": html
        }
        
        response = requests.post(google_script_url, json=payload, timeout=15)
        
        if response.status_code == 200:
            logger.info(f"✅ Informe diario enviado exitosamente a {emails_str} vía Google Apps Script")
        else:
            logger.error(f"❌ Error al enviar informe vía Webhook: {response.status_code} - {response.text}")
        
    except Exception as e:
        logger.error(f"❌ Error al generar/enviar informe diario: {e}")

@app.on_event("startup")
def start_scheduler():
    scheduler = BackgroundScheduler()
    report_time = os.getenv("REPORT_TIME", "23:59").split(":")
    h = int(report_time[0])
    m = int(report_time[1])
    scheduler.add_job(generate_and_send_daily_report, 'cron', hour=h, minute=m)
    scheduler.start()
    logger.info(f"⏰ Cron de informe diario programado para las {h:02d}:{m:02d}")