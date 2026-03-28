from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from pymongo import MongoClient
from fastapi.middleware.cors import CORSMiddleware
import os, logging

# Configurar logging para ver errores en los logs de Render
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

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
        logger.warning("MongoDB no disponible, retornando datos vacíos.")
        return {
            "inventory": [],
            "salesHistory": [],
            "usersDB": [{"user": "admin", "pass": "admin123", "role": "admin"}],
            "customersDB": [],
            "settings": {"name": store_id}
        }
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
        return {"status": "error", "message": "MongoDB no conectado"}
    try:
        store_data = data.dict()
        store_id = store_data.pop("store_id")
        collection.update_one({"_id": store_id}, {"$set": store_data}, upsert=True)
        logger.info(f"✅ Tienda '{store_id}' guardada: {len(store_data.get('inventory', []))} productos.")
        return {"status": "success", "store": store_id}
    except Exception as e:
        logger.error(f"Error guardando tienda: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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

# Servir Frontend
@app.get("/")
def serve_index():
    return FileResponse("static/index.html")

# Catálogo Público (sin login)
@app.get("/catalog/{store_id}")
def serve_public_catalog(store_id: str):
    return FileResponse("static/catalog.html")

@app.get("/catalog")
def serve_catalog_default():
    return FileResponse("static/catalog.html")

app.mount("/static", StaticFiles(directory="static"), name="static")