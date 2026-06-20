from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from pymongo import MongoClient
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
import smtplib
from email.message import EmailMessage
from datetime import datetime
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

# ── Automatización del Informe Diario ──
def generate_and_send_daily_report():
    if not MONGO_OK or collection is None:
        logger.warning("MongoDB no disponible. No se puede generar informe.")
        return
        
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    emails_str = os.getenv("REPORT_EMAILS")
    
    if not all([smtp_user, smtp_pass, emails_str]):
        logger.warning("Credenciales SMTP o REPORT_EMAILS no configuradas. Informe omitido.")
        return

    try:
        now = datetime.now()
        start_of_day = datetime(now.year, now.month, now.day).isoformat()
        
        store = collection.find_one({"_id": "main_store"})
        if not store or "salesHistory" not in store:
            logger.info("No se encontró historial de ventas para generar informe.")
            return
            
        sales = store.get("salesHistory", [])
        
        today_sales = []
        for s in sales:
            if s.get("status") != "ANULADA" and s.get("date", "") >= start_of_day:
                today_sales.append(s)
                
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
        
        msg = EmailMessage()
        msg["Subject"] = f"📊 Informe de Ventas Diario - {date_str}"
        msg["From"] = smtp_user
        msg["To"] = [e.strip() for e in emails_str.split(",")]
        msg.set_content(f"Hola, adjunto el informe de ventas del día {date_str}. Por favor revisa el contenido HTML.")
        msg.add_alternative(html, subtype="html")
        
        smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", 465))
        
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as smtp:
                smtp.login(smtp_user, smtp_pass)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as smtp:
                smtp.starttls()
                smtp.login(smtp_user, smtp_pass)
                smtp.send_message(msg)
                
        logger.info(f"✅ Informe diario enviado exitosamente a {emails_str}")
        
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