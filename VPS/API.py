import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json
import asyncpg
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from pydantic import BaseModel

load_dotenv()

# --- Настройки подключения к БД ---
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# --- Настройки авторизации ---
GOOGLE_CLIENT_ID_ANDROID = os.getenv("GOOGLE_CLIENT_ID_ANDROID")
GOOGLE_CLIENT_ID_WEB = os.getenv("GOOGLE_CLIENT_ID_WEB")
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "30"))

security = HTTPBearer()

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
# ---------- Модели ----------

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


class LookupEntry(BaseModel):
    id: int
    name: str
    description: str | None = None


class Lookups(BaseModel):
    rarities: list[LookupEntry]
    item_types: list[LookupEntry]
    special_types: list[LookupEntry]


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


# ---------- Справочники (rarities, items_type, weapons_special_types) ----------

RARITIES_QUERY = "SELECT id, name, description FROM rarities ORDER BY sort_order;"
ITEM_TYPES_QUERY = "SELECT id, name, description FROM items_type ORDER BY name;"
SPECIAL_TYPES_LOOKUP_QUERY = "SELECT id, name, description FROM weapons_special_types ORDER BY name;"


@app.get("/lookups", response_model=Lookups)
async def get_lookups():
    try:
        async with app.state.pool.acquire() as conn:
            rarities = await conn.fetch(RARITIES_QUERY)
            item_types = await conn.fetch(ITEM_TYPES_QUERY)
            special_types = await conn.fetch(SPECIAL_TYPES_LOOKUP_QUERY)
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    return Lookups(
        rarities=[LookupEntry(**dict(r)) for r in rarities],
        item_types=[LookupEntry(**dict(r)) for r in item_types],
        special_types=[LookupEntry(**dict(r)) for r in special_types],
    )

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

MAX_PAGE_SIZE = 100


def _rows_to_items(rows: list[asyncpg.Record]) -> list[ItemListEntry]:
    return [
        ItemListEntry(
            id=row["id"],
            name=row["name"],
            icon=row["icon"],
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

    # защищаемся от некорректных значений с фронта
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


# ---------- Один предмет со всеми связями ----------
# Каждая коллекция запрашивается отдельным SQL-запросом, а не одним
# гигантским LEFT JOIN — иначе несколько независимых
# многие-ко-многим таблиц (типы, пассивки, активки, заклинания и т.д.)
# перемножились бы друг с другом (Cartesian product) в одном результате.

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
SELECT wh.id, wh.hand_type, whd.damage, dt.name AS damage_type, whd.sort_order
FROM weapons_and_hands wh
LEFT JOIN weapons_and_hand_damages whd ON whd.weapon_hand_id = wh.id
LEFT JOIN damage_type dt ON dt.id = whd.damage_type_id
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
SELECT wam.damage, dt.name AS damage_type, wam.sort_order
FROM weapons_ammos wam
JOIN damage_type dt ON dt.id = wam.damage_type_id
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
        json_build_object('damage', wam.damage, 'damage_type', dt.name, 'sort_order', wam.sort_order)
        ORDER BY wam.sort_order, dt.name
    ) AS damages
    FROM weapons_ammos wam
    JOIN damage_type dt ON dt.id = wam.damage_type_id
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

            # Важно: одно соединение asyncpg выполняет запросы строго
            # по очереди, параллельно на нём запускать нельзя
            # (asyncio.gather на одном conn ломается с ошибкой
            # "another operation is in progress"). Поэтому просто
            # await по очереди — для одного предмета это доли миллисекунды.
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
    except asyncpg.PostgresError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {e}")

    return ItemDetail(
        id=base["id"],
        name=base["name"],
        description=base["description"],
        icon=base["icon"],
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
            {**dict(r), 'damages': json.loads(r['damages']) if isinstance(r['damages'], str) else r['damages']}
            for r in compatible_ammos
        ],
        passives=[dict(r) for r in passives],
        actives=[dict(r) for r in actives],
        spells=[dict(r) for r in spells],
        special_types=[dict(r) for r in special_types],
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("API:app", host="127.0.0.1", port=8000, reload=True)
