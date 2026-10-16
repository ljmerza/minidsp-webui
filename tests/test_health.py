import httpx

from app.main import app


async def test_healthz():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
