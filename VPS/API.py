import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json
import asyncpg
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Depends, UploadFile
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from pydantic import BaseModel
import hashlib
import io
from minio import Minio
from minio.error import S3Error
from PIL import Image as PILImage
from db_logger import log_change
from log_processor import process_logs
from log_processor_optimized import process_logs_optimized

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

GOOGLE_CLIENT_ID_ANDROID = os.getenv("GOOGLE_CLIENT_ID_ANDROID")
GOOGLE_CLIENT_ID_WEB = os.getenv("GOOGLE_CLIENT_ID_WEB")
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "30"))

security = HTTPBearer()

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_PUBLIC_HOST = os.getenv("MINIO_PUBLIC_HOST", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ROOT_USER")
MINIO_SECRET_KEY = os.getenv("MINIO_ROOT_PASSWORD")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "item-images")

minio_client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=False)

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_IMAGES_PER_ITEM = 5
MIN_IMAGE_SIDE = 300
MAX_IMAGE_SIDE = 3200
FULL_QUALITY = 85
THUMBNAIL_SIZE = (100, 200)
THUMBNAIL_QUALITY = 60
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}

_PUBLIC_READ_POLICY = """{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Principal": "*", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::%s/*"]}]
}""" % MINIO_BUCKET


def _image_url(object_key: str) -> str:
    return f"http://{MINIO_PUBLIC_HOST}/{MINIO_BUCKET}/{object_key}"

def _icon_url(object_key: str | None) -> str | None:
    return _image_url(object_key) if object_key else None

def _encode_webp(img: PILImage.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality)
    return buf.getvalue()

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        min_size=1,
        max_size=5,
    )
    if not minio_client.bucket_exists(MINIO_BUCKET):
        minio_client.make_bucket(MINIO_BUCKET)
    minio_client.set_bucket_policy(MINIO_BUCKET, _PUBLIC_READ_POLICY)
    yield
    await app.state.pool.close()


app = FastAPI(title="Items API", lifespan=lifespan)


class GoogleLoginRequest(BaseModel):
    id_token: str


class UserOut(BaseModel):
    id: int
    name: str | None = None
    email: str | None = None
    img: str | None = None


class AuthResponse(BaseModel):
    token: str
    user: UserOut

FIND_USER_BY_SUB_QUERY = "SELECT id, name, email, img FROM users WHERE google_sub = $1;"

INSERT_USER_QUERY = """
INSERT INTO users (google_sub, name, email, img)
VALUES ($1, $2, $3, $4)
RETURNING id, name, email, img;
"""

def _verify_google_token(token: str) -> dict:
    try:
        idinfo = google_id_token.verify_oauth2_token(token, google_requests.Request())
    except ValueError:
        raise HTTPException(status_code=401, detail="Невалидный токен Google")

    if idinfo.get("aud") not in (GOOGLE_CLIENT_ID_ANDROID, GOOGLE_CLIENT_ID_WEB):
        raise HTTPException(status_code=401, detail="Неверный audience токена")
    if idinfo.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(status_code=401, detail="Неверный issuer токена")
    return idinfo


def _create_jwt(user_id: int) -> str:
    payload = {
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> int:
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Невалидный или истёкший токен")
    return payload["user_id"]

@app.post("/auth/login", response_model=AuthResponse)
async def auth_login(body: GoogleLoginRequest):
    idinfo = _verify_google_token(body.id_token)
    sub = idinfo["sub"]
    email = idinfo.get("email", "")
    name = idinfo.get("name", "")
    img = idinfo.get("picture", "")

    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow(FIND_USER_BY_SUB_QUERY, sub)
            if row is None:
                row = await conn.fetchrow(INSERT_USER_QUERY, sub, name, email, img)
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    token = _create_jwt(row["id"])
    return AuthResponse(token=token, user=UserOut(**dict(row)))


ME_QUERY = "SELECT id, name, email, img FROM users WHERE id = $1;"

@app.get("/auth/me", response_model=UserOut)
async def auth_me(user_id: int = Depends(get_current_user_id)):
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow(ME_QUERY, user_id)
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    if row is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    return UserOut(**dict(row))



class ItemImageOut(BaseModel):
    id: int
    url: str
    sort_order: int

class ReorderImagesRequest(BaseModel):
    image_ids: list[int]

class ItemListEntry(BaseModel):
    id: int
    name: str
    icon: str | None = None
    rarity: str | None = None
    price: int
    types: list[str] = []
    special_types: list[str] = []


class PaginatedItems(BaseModel):
    items: list[ItemListEntry]
    page: int
    page_size: int
    total_count: int
    total_pages: int

class MyItemEntry(BaseModel):
    id: int
    name: str
    icon: str | None = None
    rarity: str | None = None
    price: int
    status: int


class PaginatedMyItems(BaseModel):
    items: list[MyItemEntry]
    page: int
    page_size: int
    total_count: int
    total_pages: int

class DamageRowIn(BaseModel):
    damage_type_ids: list[int] = []
    dice_multi: int | None = None
    dice_id: int | None = None
    dmg_const: int | None = None

class ItemCreateRequest(BaseModel):
    name: str
    description: str | None = None
    icon: str | None = None
    rarity_id: int
    price: int = 0
    weight: float | None = None
    item_type_id: int | None = None
    special_type_ids: list[int] = []
    one_handed_damages: list[DamageRowIn] = []
    two_handed_damages: list[DamageRowIn] = []
    ammo_item_ids: list[int] = []


class ItemCreateResponse(BaseModel):
    id: int
    status: int

class LookupEntry(BaseModel):
    id: int
    name: str
    description: str | None = None

class DiceEntry(BaseModel):
    id: int
    name: str
    sides: int

class Lookups(BaseModel):
    rarities: list[LookupEntry]
    item_types: list[LookupEntry]
    special_types: list[LookupEntry]
    damage_types: list[LookupEntry]
    dice: list[DiceEntry]


class ItemDetail(BaseModel):
    id: int
    name: str
    description: str | None = None
    icon: str | None = None
    rarity: str | None = None
    price: int
    weight: float | None = None
    types: list[dict]
    weapons_classes: list[dict]
    weapons_types: list[dict]
    hands: list[dict]
    ranges: list[dict]
    ammos: list[dict]
    compatible_ammos: list[dict]
    passives: list[dict]
    actives: list[dict]
    spells: list[dict]
    special_types: list[dict]
    images: list[dict]

class ItemUpdateRequest(BaseModel):
    name: str
    description: str | None = None
    icon: str | None = None
    rarity_id: int
    price: int = 0
    weight: float | None = None
    special_type_ids: list[int] = []
    one_handed_damages: list[DamageRowIn] = []
    two_handed_damages: list[DamageRowIn] = []
    ammo_item_ids: list[int] = []


class ItemUpdateResponse(BaseModel):
    id: int
    status: int



RARITIES_QUERY = "SELECT id, name, description FROM rarities ORDER BY sort_order;"
ITEM_TYPES_QUERY = "SELECT id, name, description FROM items_type ORDER BY name;"
SPECIAL_TYPES_LOOKUP_QUERY = "SELECT id, name, description FROM weapons_special_types ORDER BY name;"
DAMAGE_TYPES_QUERY = "SELECT id, name, description FROM damage_type ORDER BY name;"
DICE_QUERY = "SELECT id, name, sides FROM dice ORDER BY sides;"



@app.get("/lookups", response_model=Lookups)
async def get_lookups():
    try:
        async with app.state.pool.acquire() as conn:
            rarities = await conn.fetch(RARITIES_QUERY)
            item_types = await conn.fetch(ITEM_TYPES_QUERY)
            special_types = await conn.fetch(SPECIAL_TYPES_LOOKUP_QUERY)
            damage_types = await conn.fetch(DAMAGE_TYPES_QUERY)
            dice = await conn.fetch(DICE_QUERY)
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    return Lookups(
        rarities=[LookupEntry(**dict(r)) for r in rarities],
        item_types=[LookupEntry(**dict(r)) for r in item_types],
        special_types=[LookupEntry(**dict(r)) for r in special_types],
        damage_types=[LookupEntry(**dict(r)) for r in damage_types],
        dice=[DiceEntry(**dict(r)) for r in dice],
    )

COUNT_IMAGES_QUERY = "SELECT COUNT(*) FROM items_images WHERE item_id = $1;"
INSERT_IMAGE_QUERY = """
INSERT INTO items_images (item_id, object_key, thumbnail_key, sort_order)
VALUES ($1, $2, $3, $4)
RETURNING id, sort_order;
"""
IMAGES_QUERY = "SELECT id, object_key, sort_order FROM items_images WHERE item_id = $1 ORDER BY sort_order;"
DELETE_IMAGE_QUERY = "DELETE FROM items_images WHERE id = $1 AND item_id = $2 RETURNING object_key, thumbnail_key;"
FIRST_IMAGE_QUERY = "SELECT thumbnail_key FROM items_images WHERE item_id = $1 ORDER BY sort_order LIMIT 1;"
UPDATE_ITEM_ICON_QUERY = "UPDATE items SET icon = $2 WHERE id = $1;"
REORDER_IMAGE_QUERY = "UPDATE items_images SET sort_order = $3 WHERE id = $1 AND item_id = $2;"


LIST_QUERY = """
SELECT
    i.id,
    i.name,
    i.icon,
    r.name AS rarity,
    i.price,
    COALESCE(
        array_agg(DISTINCT t.name) FILTER (WHERE t.name IS NOT NULL),
        '{}'
    ) AS types,
    COALESCE(
        array_agg(DISTINCT wst.name) FILTER (WHERE wst.name IS NOT NULL),
        '{}'
    ) AS special_types,
    COUNT(*) OVER() AS total_count
FROM items i
LEFT JOIN rarities r ON r.id = i.rarity_id
LEFT JOIN items_and_types iat ON iat.item_id = i.id
LEFT JOIN items_type t ON t.id = iat.item_type_id
LEFT JOIN weapons_and_special_types wast ON wast.item_id = i.id
LEFT JOIN weapons_special_types wst ON wst.id = wast.special_type_id
WHERE
    ($1::bigint[] IS NULL OR i.rarity_id = ANY($1::bigint[]))
    AND ($2::integer IS NULL OR i.price >= $2)
    AND ($3::integer IS NULL OR i.price <= $3)
    AND (
        $4::bigint[] IS NULL OR EXISTS (
            SELECT 1
            FROM items_and_types iat_f
            WHERE iat_f.item_id = i.id
              AND iat_f.item_type_id = ANY($4::bigint[])
        )
    )
    AND (
        $5::bigint[] IS NULL OR EXISTS (
            SELECT 1
            FROM weapons_and_special_types wst_f
            WHERE wst_f.item_id = i.id
              AND wst_f.special_type_id = ANY($5::bigint[])
        )
    )
GROUP BY i.id, i.name, i.icon, r.name, i.price
ORDER BY i.name
LIMIT $6 OFFSET $7;
"""

SEARCH_QUERY = """
SELECT
    i.id,
    i.name,
    i.icon,
    r.name AS rarity,
    i.price,
    COALESCE(
        array_agg(DISTINCT t.name) FILTER (WHERE t.name IS NOT NULL),
        '{}'
    ) AS types,
    COALESCE(
        array_agg(DISTINCT wst.name) FILTER (WHERE wst.name IS NOT NULL),
        '{}'
    ) AS special_types,
    COUNT(*) OVER() AS total_count
FROM items i
LEFT JOIN rarities r ON r.id = i.rarity_id
LEFT JOIN items_and_types iat ON iat.item_id = i.id
LEFT JOIN items_type t ON t.id = iat.item_type_id
LEFT JOIN weapons_and_special_types wast ON wast.item_id = i.id
LEFT JOIN weapons_special_types wst ON wst.id = wast.special_type_id
WHERE
    i.name ILIKE '%' || $1 || '%'
    AND ($2::bigint[] IS NULL OR i.rarity_id = ANY($2::bigint[]))
    AND ($3::integer IS NULL OR i.price >= $3)
    AND ($4::integer IS NULL OR i.price <= $4)
    AND (
        $5::bigint[] IS NULL OR EXISTS (
            SELECT 1
            FROM items_and_types iat_f
            WHERE iat_f.item_id = i.id
              AND iat_f.item_type_id = ANY($5::bigint[])
        )
    )
    AND (
        $6::bigint[] IS NULL OR EXISTS (
            SELECT 1
            FROM weapons_and_special_types wst_f
            WHERE wst_f.item_id = i.id
              AND wst_f.special_type_id = ANY($6::bigint[])
        )
    )
GROUP BY i.id, i.name, i.icon, r.name, i.price
ORDER BY i.name
LIMIT $7 OFFSET $8;
"""

MY_ITEMS_QUERY = """
SELECT
    i.id,
    i.name,
    i.icon,
    r.name AS rarity,
    i.price,
    uai.status,
    COUNT(*) OVER() AS total_count
FROM users_and_items uai
JOIN items i ON i.id = uai.item_id
LEFT JOIN rarities r ON r.id = i.rarity_id
WHERE uai.user_id = $1
ORDER BY i.name
LIMIT $2 OFFSET $3;
"""

INSERT_ITEM_QUERY = """
INSERT INTO items (name, description, icon, rarity_id, price, weight)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING id;
"""

INSERT_ITEM_TYPE_QUERY = """
INSERT INTO items_and_types (item_id, item_type_id)
VALUES ($1, $2);
"""

INSERT_SPECIAL_TYPE_QUERY = """
INSERT INTO weapons_and_special_types (item_id, special_type_id)
VALUES ($1, $2);
"""

INSERT_HAND_QUERY = """
INSERT INTO weapons_and_hands (item_id, hand_type)
VALUES ($1, $2)
RETURNING id;
"""

INSERT_HAND_DAMAGE_QUERY = """
INSERT INTO weapons_and_hand_damages (weapon_hand_id, damage_type_id, sort_order, dice_multi, dice_id, dmg_const)
VALUES ($1, $2, $3, $4, $5, $6);
"""

INSERT_WEAPON_AMMO_QUERY = """
INSERT INTO weapons_and_ammos (weapon_item_id, ammo_item_id)
VALUES ($1, $2);
"""

INSERT_USER_ITEM_QUERY = """
INSERT INTO users_and_items (user_id, item_id, status)
VALUES ($1, $2, 0)
RETURNING status;
"""

CHECK_OWNERSHIP_QUERY = "SELECT 1 FROM users_and_items WHERE user_id = $1 AND item_id = $2;"

DELETE_ITEM_QUERY = "DELETE FROM items WHERE id = $1;"

UPDATE_ITEM_QUERY = """
UPDATE items SET name = $2, description = $3, icon = $4, rarity_id = $5, price = $6, weight = $7
WHERE id = $1;
"""

DELETE_SPECIAL_TYPES_QUERY = "DELETE FROM weapons_and_special_types WHERE item_id = $1;"
DELETE_HANDS_QUERY = "DELETE FROM weapons_and_hands WHERE item_id = $1;"
DELETE_WEAPON_AMMO_QUERY = "DELETE FROM weapons_and_ammos WHERE weapon_item_id = $1;"

RESET_STATUS_QUERY = """
UPDATE users_and_items SET status = 0 WHERE user_id = $1 AND item_id = $2
RETURNING status;
"""

MAX_PAGE_SIZE = 100


def _rows_to_items(rows: list[asyncpg.Record]) -> list[ItemListEntry]:
    return [
        ItemListEntry(
            id=row["id"],
            name=row["name"],
            icon=_icon_url(row["icon"]),
            rarity=row["rarity"],
            price=row["price"],
            types=row["types"],
            special_types=row["special_types"],
        )
        for row in rows
    ]


@app.get("/items/search", response_model=PaginatedItems)
async def search_items(
    q: str,
    page: int = 1,
    page_size: int = 100,
    rarity_id: list[int] | None = Query(None, description="ID редкости (можно несколько)"),
    item_type_id: list[int] | None = Query(None, description="ID типа предмета (можно несколько)"),
    special_type_id: list[int] | None = Query(None, description="ID особого типа оружия (можно несколько)"),
    price_min: int | None = Query(None, ge=0),
    price_max: int | None = Query(None, ge=0),
):
    q = q.strip()

    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    offset = (page - 1) * page_size

    if price_min is not None and price_max is not None and price_min > price_max:
        raise HTTPException(status_code=400, detail="price_min не может быть больше price_max")

    if not q:
        return PaginatedItems(items=[], page=page, page_size=page_size, total_count=0, total_pages=0)

    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                SEARCH_QUERY,
                q,
                rarity_id,
                price_min,
                price_max,
                item_type_id,
                special_type_id,
                page_size,
                offset,
            )
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    total_count = rows[0]["total_count"] if rows else 0
    total_pages = (total_count + page_size - 1) // page_size if total_count else 0

    return PaginatedItems(
        items=_rows_to_items(rows),
        page=page,
        page_size=page_size,
        total_count=total_count,
        total_pages=total_pages,
    )


@app.get("/items", response_model=PaginatedItems)
async def get_items(
    page: int = 1,
    page_size: int = 100,
    rarity_id: list[int] | None = Query(None, description="ID редкости (можно несколько)"),
    item_type_id: list[int] | None = Query(None, description="ID типа предмета (можно несколько)"),
    special_type_id: list[int] | None = Query(None, description="ID особого типа оружия (можно несколько)"),
    price_min: int | None = Query(None, ge=0),
    price_max: int | None = Query(None, ge=0),
):
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    offset = (page - 1) * page_size

    if price_min is not None and price_max is not None and price_min > price_max:
        raise HTTPException(status_code=400, detail="price_min не может быть больше price_max")

    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                LIST_QUERY,
                rarity_id,
                price_min,
                price_max,
                item_type_id,
                special_type_id,
                page_size,
                offset,
            )
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    total_count = rows[0]["total_count"] if rows else 0
    total_pages = (total_count + page_size - 1) // page_size if total_count else 0

    return PaginatedItems(
        items=_rows_to_items(rows),
        page=page,
        page_size=page_size,
        total_count=total_count,
        total_pages=total_pages,
    )



BASE_ITEM_QUERY = """
SELECT i.id, i.name, i.description, i.icon, i.price, i.weight, r.name AS rarity
FROM items i
LEFT JOIN rarities r ON r.id = i.rarity_id
WHERE i.id = $1;
"""

TYPES_QUERY = """
SELECT t.id, t.name
FROM items_and_types iat
JOIN items_type t ON t.id = iat.item_type_id
WHERE iat.item_id = $1
ORDER BY t.name;
"""

WEAPON_CLASSES_QUERY = """
SELECT wc.id, wc.name
FROM items_and_weapons_classes iwc
JOIN weapons_classes wc ON wc.id = iwc.weapon_class_id
WHERE iwc.item_id = $1
ORDER BY wc.name;
"""

WEAPON_TYPES_QUERY = """
SELECT wt.id, wt.name
FROM items_and_weapons_types iwt
JOIN weapons_types wt ON wt.id = iwt.weapon_type_id
WHERE iwt.item_id = $1
ORDER BY wt.name;
"""

HANDS_QUERY = """
SELECT wh.id, wh.hand_type, whd.dice_multi, d.name AS dice_name, whd.dmg_const, dt.name AS damage_type, whd.sort_order
FROM weapons_and_hands wh
LEFT JOIN weapons_and_hand_damages whd ON whd.weapon_hand_id = wh.id
LEFT JOIN damage_type dt ON dt.id = whd.damage_type_id
LEFT JOIN dice d ON d.id = whd.dice_id
WHERE wh.item_id = $1
ORDER BY wh.id, whd.sort_order, dt.name;
"""

RANGES_QUERY = """
SELECT range_type, min_range, max_range
FROM weapons_and_ranges
WHERE item_id = $1
ORDER BY id;
"""

AMMOS_QUERY = """
SELECT wam.dice_multi, d.name AS dice_name, wam.dmg_const, dt.name AS damage_type, wam.sort_order
FROM weapons_ammos wam
JOIN damage_type dt ON dt.id = wam.damage_type_id
JOIN dice d ON d.id = wam.dice_id
WHERE wam.item_id = $1
ORDER BY wam.sort_order, dt.name;
"""

COMPATIBLE_AMMOS_QUERY = """
SELECT
    i.id,
    i.name,
    i.icon,
    wr.min_range,
    wr.max_range,
    COALESCE(dmg.damages, '[]') AS damages
FROM weapons_and_ammos wa
JOIN items i ON i.id = wa.ammo_item_id
LEFT JOIN weapons_and_ranges wr ON wr.item_id = i.id
LEFT JOIN LATERAL (
    SELECT json_agg(
        json_build_object('dice_multi', wam.dice_multi, 'dice_name', d.name, 'dmg_const', wam.dmg_const, 'damage_type', dt.name, 'sort_order', wam.sort_order)
        ORDER BY wam.sort_order, dt.name
    ) AS damages
    FROM weapons_ammos wam
    JOIN damage_type dt ON dt.id = wam.damage_type_id
    JOIN dice d ON d.id = wam.dice_id
    WHERE wam.item_id = i.id
) dmg ON true
WHERE wa.weapon_item_id = $1
ORDER BY i.name;
"""

PASSIVES_QUERY = """
SELECT p.id, p.name, p.description, p.cooldown
FROM items_and_passive_abilities wp
JOIN items_passive p ON p.id = wp.passive_id
WHERE wp.item_id = $1
ORDER BY p.name;
"""

ACTIVES_QUERY = """
SELECT a.id, a.name, a.description, a.count, a.cooldown, a.requirement,
       wia.is_default
FROM items_and_active_abilities wia
JOIN items_actives a ON a.id = wia.item_active_id
WHERE wia.item_id = $1
ORDER BY a.name;
"""

SPELLS_QUERY = """
SELECT ws.id AS weapon_spell_id, ws.name AS weapon_spell_name,
       ws.description, ws.count,
       s.id AS spell_id, s.name AS spell_name, s.level, s.school
FROM weapons_and_spells was
JOIN items_spells ws ON ws.id = was.spell_id
LEFT JOIN spells s ON s.id = ws.spell_id
WHERE was.item_id = $1
ORDER BY ws.name;
"""

SPECIAL_TYPES_QUERY = """
SELECT st.id, st.name
FROM weapons_and_special_types wst
JOIN weapons_special_types st ON st.id = wst.special_type_id
WHERE wst.item_id = $1
ORDER BY st.name;
"""


@app.get("/items/{item_id}", response_model=ItemDetail)
async def get_item(item_id: int):
    try:
        async with app.state.pool.acquire() as conn:
            base = await conn.fetchrow(BASE_ITEM_QUERY, item_id)
            if base is None:
                raise HTTPException(status_code=404, detail="Предмет не найден")
            types = await conn.fetch(TYPES_QUERY, item_id)
            weapons_classes = await conn.fetch(WEAPON_CLASSES_QUERY, item_id)
            weapons_types = await conn.fetch(WEAPON_TYPES_QUERY, item_id)
            hands = await conn.fetch(HANDS_QUERY, item_id)
            ranges = await conn.fetch(RANGES_QUERY, item_id)
            ammos = await conn.fetch(AMMOS_QUERY, item_id)
            compatible_ammos = await conn.fetch(COMPATIBLE_AMMOS_QUERY, item_id)
            passives = await conn.fetch(PASSIVES_QUERY, item_id)
            actives = await conn.fetch(ACTIVES_QUERY, item_id)
            spells = await conn.fetch(SPELLS_QUERY, item_id)
            special_types = await conn.fetch(SPECIAL_TYPES_QUERY, item_id)
            images = await conn.fetch(IMAGES_QUERY, item_id)
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    return ItemDetail(
        id=base["id"],
        name=base["name"],
        description=base["description"],
        icon=_icon_url(base["icon"]),
        rarity=base["rarity"],
        price=base["price"],
        weight=float(base["weight"]) if base["weight"] is not None else None,
        types=[dict(r) for r in types],
        weapons_classes=[dict(r) for r in weapons_classes],
        weapons_types=[dict(r) for r in weapons_types],
        hands=[dict(r) for r in hands],
        ranges=[dict(r) for r in ranges],
        ammos=[dict(r) for r in ammos],
        compatible_ammos=[
            {
                **dict(r),
                'icon': _icon_url(r['icon']),
                'damages': json.loads(r['damages']) if isinstance(r['damages'], str) else r['damages'],
            }
            for r in compatible_ammos
        ],
        passives=[dict(r) for r in passives],
        actives=[dict(r) for r in actives],
        spells=[dict(r) for r in spells],
        special_types=[dict(r) for r in special_types],
        images=[{"id": r["id"], "url": _image_url(r["object_key"]), "sort_order": r["sort_order"]} for r in images]
    )

@app.get("/users/me/items", response_model=PaginatedMyItems)
async def get_my_items(
    page: int = 1,
    page_size: int = 100,
    user_id: int = Depends(get_current_user_id),
):
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    offset = (page - 1) * page_size

    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(MY_ITEMS_QUERY, user_id, page_size, offset)
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    total_count = rows[0]["total_count"] if rows else 0
    total_pages = (total_count + page_size - 1) // page_size if total_count else 0

    return PaginatedMyItems(
        items=[
            MyItemEntry(id=r["id"], name=r["name"], icon=_icon_url(r["icon"]), rarity=r["rarity"], price=r["price"],
                        status=r["status"])
            for r in rows
        ],
        page=page,
        page_size=page_size,
        total_count=total_count,
        total_pages=total_pages,
    )


@app.post("/items", response_model=ItemCreateResponse)
async def create_item(body: ItemCreateRequest, user_id: int = Depends(get_current_user_id)):
    item_id = None
    try:
        async with app.state.pool.acquire() as conn:
            async with conn.transaction():
                item_row = await conn.fetchrow(
                    INSERT_ITEM_QUERY, body.name, body.description, body.icon, body.rarity_id, body.price, body.weight
                )
                item_id = item_row["id"]

                if body.item_type_id is not None:
                    await conn.execute(INSERT_ITEM_TYPE_QUERY, item_id, body.item_type_id)

                for special_type_id in body.special_type_ids:
                    await conn.execute(INSERT_SPECIAL_TYPE_QUERY, item_id, special_type_id)

                for hand_type, rows in ((False, body.one_handed_damages), (True, body.two_handed_damages)):
                    if not rows:
                        continue
                    hand_row = await conn.fetchrow(INSERT_HAND_QUERY, item_id, hand_type)
                    hand_id = hand_row["id"]
                    for sort_order, row in enumerate(rows):
                        for damage_type_id in row.damage_type_ids:
                            await conn.execute(
                                INSERT_HAND_DAMAGE_QUERY,
                                hand_id, damage_type_id, sort_order, row.dice_multi, row.dice_id, row.dmg_const,
                            )

                for ammo_item_id in body.ammo_item_ids:
                    await conn.execute(INSERT_WEAPON_AMMO_QUERY, item_id, ammo_item_id)

                link_row = await conn.fetchrow(INSERT_USER_ITEM_QUERY, user_id, item_id)
    except asyncpg.PostgresError as e:
        log_change(user_id=user_id, action="INSERT", row_id=item_id or 0, status="ERROR")
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    log_change(user_id=user_id, action="INSERT", row_id=item_id, status="OK")
    return ItemCreateResponse(id=item_id, status=link_row["status"])

@app.delete("/items/{item_id}")
async def delete_item(item_id: int, user_id: int = Depends(get_current_user_id)):
    try:
        async with app.state.pool.acquire() as conn:
            owns = await conn.fetchval(CHECK_OWNERSHIP_QUERY, user_id, item_id)
            if not owns:
                log_change(user_id=user_id, action="DELETE", row_id=item_id, status="ERROR")
                raise HTTPException(status_code=403, detail="Вы не являетесь владельцем этого предмета")
            await conn.execute(DELETE_ITEM_QUERY, item_id)
    except asyncpg.PostgresError as e:
        log_change(user_id=user_id, action="DELETE", row_id=item_id, status="ERROR")
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    log_change(user_id=user_id, action="DELETE", row_id=item_id, status="OK")
    return {"status": "deleted"}


@app.patch("/items/{item_id}", response_model=ItemUpdateResponse)
async def update_item(item_id: int, body: ItemUpdateRequest, user_id: int = Depends(get_current_user_id)):
    try:
        async with app.state.pool.acquire() as conn:
            owns = await conn.fetchval(CHECK_OWNERSHIP_QUERY, user_id, item_id)
            if not owns:
                log_change(user_id=user_id, action="UPDATE", row_id=item_id, status="ERROR")
                raise HTTPException(status_code=403, detail="Вы не являетесь владельцем этого предмета")

            async with conn.transaction():
                await conn.execute(
                    UPDATE_ITEM_QUERY, item_id, body.name, body.description, body.icon, body.rarity_id, body.price, body.weight
                )

                await conn.execute(DELETE_SPECIAL_TYPES_QUERY, item_id)
                for special_type_id in body.special_type_ids:
                    await conn.execute(INSERT_SPECIAL_TYPE_QUERY, item_id, special_type_id)

                await conn.execute(DELETE_HANDS_QUERY, item_id)
                for hand_type, rows in ((False, body.one_handed_damages), (True, body.two_handed_damages)):
                    if not rows:
                        continue
                    hand_row = await conn.fetchrow(INSERT_HAND_QUERY, item_id, hand_type)
                    hand_id = hand_row["id"]
                    for sort_order, row in enumerate(rows):
                        for damage_type_id in row.damage_type_ids:
                            await conn.execute(
                                INSERT_HAND_DAMAGE_QUERY,
                                hand_id, damage_type_id, sort_order, row.dice_multi, row.dice_id, row.dmg_const,
                            )

                await conn.execute(DELETE_WEAPON_AMMO_QUERY, item_id)
                for ammo_item_id in body.ammo_item_ids:
                    await conn.execute(INSERT_WEAPON_AMMO_QUERY, item_id, ammo_item_id)

                status_row = await conn.fetchrow(RESET_STATUS_QUERY, user_id, item_id)

    except asyncpg.PostgresError as e:
        log_change(user_id=user_id, action="UPDATE", row_id=item_id, status="ERROR")
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    log_change(user_id=user_id, action="UPDATE", row_id=item_id, status="OK")
    return ItemUpdateResponse(id=item_id, status=status_row["status"])

@app.post("/items/{item_id}/images", response_model=ItemImageOut)
async def upload_item_image(item_id: int, file: UploadFile, user_id: int = Depends(get_current_user_id)):
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Разрешены только JPEG, PNG, WEBP")

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Файл больше 30 МБ")

    try:
        img = PILImage.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(status_code=400, detail="Файл повреждён или не является изображением")

    if img.width < MIN_IMAGE_SIDE or img.height < MIN_IMAGE_SIDE:
        raise HTTPException(status_code=400, detail=f"Минимальное разрешение изображения {MIN_IMAGE_SIDE}x{MIN_IMAGE_SIDE}")

    img = img.convert("RGB")

    full_img = img.copy()
    full_img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    full_bytes = _encode_webp(full_img, FULL_QUALITY)
    object_key = hashlib.sha256(full_bytes).hexdigest()[:24] + ".webp"

    thumb_img = img.copy()
    thumb_img.thumbnail(THUMBNAIL_SIZE)
    thumb_bytes = _encode_webp(thumb_img, THUMBNAIL_QUALITY)
    thumbnail_key = hashlib.sha256(thumb_bytes).hexdigest()[:24] + "_thumb.webp"

    try:
        async with app.state.pool.acquire() as conn:
            count = await conn.fetchval(COUNT_IMAGES_QUERY, item_id)
            if count >= MAX_IMAGES_PER_ITEM:
                raise HTTPException(status_code=400, detail=f"Не более {MAX_IMAGES_PER_ITEM} изображений на предмет")

            minio_client.put_object(MINIO_BUCKET, object_key, io.BytesIO(full_bytes), length=len(full_bytes), content_type="image/webp")
            minio_client.put_object(MINIO_BUCKET, thumbnail_key, io.BytesIO(thumb_bytes), length=len(thumb_bytes), content_type="image/webp")
            row = await conn.fetchrow(INSERT_IMAGE_QUERY, item_id, object_key, thumbnail_key, count)
            if count == 0:
                await conn.execute(UPDATE_ITEM_ICON_QUERY, item_id, thumbnail_key)
    except S3Error as e:
        raise HTTPException(status_code=500, detail=f"Ошибка хранилища: {e}")
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    return ItemImageOut(id=row["id"], url=_image_url(object_key), sort_order=row["sort_order"])


@app.delete("/items/{item_id}/images/{image_id}")
async def delete_item_image(item_id: int, image_id: int, user_id: int = Depends(get_current_user_id)):
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow(DELETE_IMAGE_QUERY, image_id, item_id)
            if row is not None:
                new_first = await conn.fetchval(FIRST_IMAGE_QUERY, item_id)
                await conn.execute(UPDATE_ITEM_ICON_QUERY, item_id, new_first)
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    if row is None:
        raise HTTPException(status_code=404, detail="Изображение не найдено")
    try:
        minio_client.remove_object(MINIO_BUCKET, row["object_key"])
        if row["thumbnail_key"]:
            minio_client.remove_object(MINIO_BUCKET, row["thumbnail_key"])
    except S3Error:
        pass
    return {"status": "ok"}


@app.patch("/items/{item_id}/images/reorder")
async def reorder_item_images(item_id: int, body: ReorderImagesRequest, user_id: int = Depends(get_current_user_id)):
    try:
        async with app.state.pool.acquire() as conn:
            async with conn.transaction():
                for sort_order, image_id in enumerate(body.image_ids):
                    await conn.execute(REORDER_IMAGE_QUERY, image_id, item_id, sort_order)
                new_first = await conn.fetchval(FIRST_IMAGE_QUERY, item_id)
                await conn.execute(UPDATE_ITEM_ICON_QUERY, item_id, new_first)
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")
    return {"status": "ok"}

@app.get("/logs")
async def get_logs():
    log_lines, result_lines = process_logs()
    return {
        "logs": log_lines,
        "result": result_lines,
    }

@app.get("/logs/optimized")
async def get_logs_optimized():
    """Оптимизированный обработчик: O(N) вместо O(N²)."""
    log_lines, result_lines = process_logs_optimized()
    return {
        "logs": log_lines,
        "result": result_lines,
    }

@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("API:app", host="127.0.0.1", port=8000, reload=True)
