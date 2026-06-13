import os
import re
import unicodedata
import pymongo
import requests
from pathlib import Path

# Configuración
MONGO_URL = "mongodb+srv://admin_tupac:TupacAdmin2026@cluster0.urq0mp6.mongodb.net/?appName=Cluster0"
IMAGES_DIR = r"C:\Users\Frelin\Desktop\mi_enjambre_ia\fotos_para_kyte"

import sys
import codecs

# Fix print encoding for Windows console (emojis)
sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')

def slugify(text):
    """Convierte el texto a un formato normalizado (minúsculas, sin acentos, sin caracteres especiales)"""
    if not text:
        return ""
    # Quitar acentos
    text = unicodedata.normalize('NFKD', text).encode('ASCII', 'ignore').decode('utf-8')
    text = text.lower()
    # Eliminar TODO lo que no sea letra o número
    text = re.sub(r'[^a-z0-9]', '', text)
    return text

def main(dry_run=True):
    print("Conectando a MongoDB...")
    client = pymongo.MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client['capsule_cloud_db']
    collection = db['stores']
    
    doc = collection.find_one({'_id': 'main_store'})
    if not doc:
        print("Error: No se encontró la tienda 'main_store' en MongoDB.")
        return

    settings = doc.get('settings', {})
    cloud_name = settings.get('cloudinaryName')
    upload_preset = settings.get('cloudinaryPreset')
    inventory = doc.get('inventory', [])
    
    if not cloud_name or not upload_preset:
        print("Error: No se encontró la configuración de Cloudinary en la base de datos.")
        return
        
    print(f"Productos en inventario: {len(inventory)}")
    print(f"Cloudinary Config: Name={cloud_name}, Preset={upload_preset}")
    
    if not os.path.exists(IMAGES_DIR):
        print(f"Error: La carpeta {IMAGES_DIR} no existe.")
        return

    # Crear diccionario de productos normalizados
    products_map = {}
    for prod in inventory:
        slug = slugify(prod.get('name', ''))
        if slug:
            products_map[slug] = prod

    files = [f for f in os.listdir(IMAGES_DIR) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))]
    print(f"Imágenes encontradas en la carpeta: {len(files)}")
    
    updates = 0
    matches_found = 0
    
    for filename in files:
        # Extraer el nombre base y el sufijo (ej: _1, _2, _3)
        # Abrazadera_Arca_Swiss_Profesional_para_Tripode_1.jpg -> base, _1
        name_without_ext = os.path.splitext(filename)[0]
        
        # Buscar si termina en _1, _2, o _3
        match = re.search(r'_([123])$', name_without_ext)
        img_index = 1
        if match:
            img_index = int(match.group(1))
            base_name = name_without_ext[:-2] # Quitar el _1
        else:
            base_name = name_without_ext
            
        file_slug = slugify(base_name)
        
        # Buscar el producto coincidente
        matched_prod = None
        
        # Intento 1: Coincidencia exacta
        if file_slug in products_map:
            matched_prod = products_map[file_slug]
        else:
            # Intento 2: Coincidencia parcial
            for prod_slug, prod in products_map.items():
                if file_slug in prod_slug or prod_slug in file_slug:
                    matched_prod = prod
                    break
                    
        if matched_prod:
            matches_found += 1
            prod_id = matched_prod['id']
            field_name = f'img{img_index}'
            
            # Ver si el producto ya tiene esta imagen
            current_url = matched_prod.get(field_name, '')
            if current_url and not dry_run:
                # Opcional: saltar si ya tiene URL (descomentar si se desea)
                # print(f"[{matched_prod['name']}] Ya tiene {field_name}, saltando.")
                pass
                
            print(f"Match: '{filename}' -> '{matched_prod['name']}' (Campo: {field_name})")
            
            if not dry_run:
                print(f"  Subiendo {filename} a Cloudinary...")
                file_path = os.path.join(IMAGES_DIR, filename)
                url = f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"
                with open(file_path, 'rb') as f:
                    response = requests.post(url, data={'upload_preset': upload_preset}, files={'file': f})
                    
                if response.status_code == 200:
                    secure_url = response.json().get('secure_url')
                    print(f"  Éxito: {secure_url}")
                    
                    # Actualizar en memoria
                    matched_prod[field_name] = secure_url
                    updates += 1
                else:
                    print(f"  Error subiendo: {response.text}")
        else:
            print(f"Sin coincidencia para: {filename} (Slug buscado: {file_slug})")
            
    if dry_run:
        print(f"\n--- MODO PRUEBA FINALIZADO ---")
        print(f"Se encontraron {matches_found} coincidencias de {len(files)} imágenes.")
        print("Cambia dry_run=False en main() para ejecutar la subida real.")
    else:
        if updates > 0:
            print(f"\nGuardando {updates} imágenes en MongoDB...")
            collection.update_one({'_id': 'main_store'}, {'$set': {'inventory': inventory}})
            print("¡Actualización completada!")
        else:
            print("No hubo nuevas imágenes para actualizar.")

if __name__ == "__main__":
    # Ejecutamos la subida real a Cloudinary y MongoDB
    main(dry_run=False)
