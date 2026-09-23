def test_health(api_get):
    r = api_get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_content_type(api_get):
    r = api_get("/health")
    assert r.headers["content-type"].startswith("application/json")


def test_lookups_status(api_get):
    r = api_get("/lookups")
    assert r.status_code == 200


def test_lookups_structure(api_get):
    r = api_get("/lookups")
    data = r.json()
    assert set(data.keys()) == {
        "rarities", "item_types", "special_types", "damage_types", "dice"
    }
    assert isinstance(data["rarities"], list)
    assert isinstance(data["item_types"], list)
    assert isinstance(data["damage_types"], list)
    assert isinstance(data["dice"], list)


def test_lookups_has_data(api_get):
    r = api_get("/lookups")
    data = r.json()
    assert len(data["rarities"]) > 0
    assert len(data["damage_types"]) > 0
    assert len(data["dice"]) > 0


def test_items_status(api_get):
    r = api_get("/items")
    assert r.status_code == 200


def test_items_structure(api_get):
    r = api_get("/items")
    data = r.json()
    assert "items" in data
    assert "page" in data
    assert "page_size" in data
    assert "total_count" in data
    assert "total_pages" in data


def test_items_has_data(api_get):
    r = api_get("/items")
    data = r.json()
    assert data["total_count"] > 0
    assert len(data["items"]) > 0


def test_items_pagination(api_get):
    r = api_get("/items?page=1&page_size=5")
    data = r.json()
    assert data["page"] == 1
    assert data["page_size"] == 5
    assert len(data["items"]) <= 5


def test_items_page_size_capped(api_get):
    r = api_get("/items?page_size=9999")
    assert r.json()["page_size"] == 100


def test_search_empty_returns_empty(api_get):
    r = api_get("/items/search?q=")
    assert r.status_code == 200
    data = r.json()
    assert data["items"] == []
    assert data["total_count"] == 0


def test_search_finds_sword(api_get):
    r = api_get("/items/search?q=меч")
    assert r.status_code == 200
    data = r.json()
    assert data["total_count"] > 0


def test_get_item_ok(api_get):
    items = api_get("/items?page_size=1").json()["items"]
    assert len(items) > 0
    item_id = items[0]["id"]

    r = api_get(f"/items/{item_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == item_id
    assert "name" in data
    assert "price" in data


def test_get_item_404(api_get):
    r = api_get("/items/99999999")
    assert r.status_code == 404


def test_docs_available(api_get):
    r = api_get("/docs")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_openapi_schema(api_get):
    r = api_get("/openapi.json")
    assert r.status_code == 200
    data = r.json()
    assert "paths" in data
    assert "/health" in data["paths"]
    assert "/items" in data["paths"]
    assert "/lookups" in data["paths"]
    assert "/logs" in data["paths"]


def test_logs(api_get):
    r = api_get("/logs")
    assert r.status_code == 200
    data = r.json()
    assert "logs" in data
    assert "result" in data
    assert isinstance(data["logs"], list)
    assert isinstance(data["result"], list)

def test_logs_optimized(api_get):
    r = api_get("/logs/optimized")
    assert r.status_code == 200
    data = r.json()
    assert "logs" in data
    assert "result" in data
    assert isinstance(data["logs"], list)
    assert isinstance(data["result"], list)