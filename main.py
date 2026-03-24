from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pymongo import MongoClient
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Conexión MongoDB (Render usará la variable de entorno MONGO_URL)
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(MONGO_URL)
db = client["capsule_cloud_db"]
collection = db["stores"]

class AppData(BaseModel):
    store_id: str  # <--- Identificador único de la tienda
    inventory: list = []
    salesHistory: list = []
    usersDB: list = []
    customersDB: list = []
    settings: dict = {}

@app.get("/api/data/{store_id}")
def get_data(store_id: str):
    data = collection.find_one({"_id": store_id})
    if not data:
        # Retornamos estructura inicial si la tienda es nueva
        return {
            "inventory": [], 
            "salesHistory": [], 
            "usersDB": [{"user": "admin", "pass": "admin123", "role": "admin"}], 
            "customersDB": [], 
            "settings": {"name": store_id}
        }
    return data

@app.post("/api/data")
def save_data(data: AppData):
    # Usamos store_id como la llave primaria (_id) en MongoDB
    store_data = data.dict()
    store_id = store_data.pop("store_id")
    collection.update_one({"_id": store_id}, {"$set": store_data}, upsert=True)
    return {"status": "success", "store": store_id}

# Servir Frontend
@app.get("/")
def serve_index():
    return FileResponse("static/index.html")

app.mount("/static", StaticFiles(directory="static"), name="static")