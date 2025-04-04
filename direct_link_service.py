from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from p123 import P123Client
from p123.tool import get_downurl
import os

# 从环境变量读取凭证
client = P123Client(
    passport=os.getenv("P123_PASSPORT"),
    password=os.getenv("P123_PASSWORD")
)

app = FastAPI(debug=os.getenv("DEBUG", "false").lower() == "true")

@app.get("/{uri:path}")
@app.head("/{uri:path}")
async def index(request: Request, uri: str):
    try:
        payload = int(uri)
    except ValueError:
        if uri.count("|") < 2:
            return JSONResponse({"state": False, "message": f"bad uri: {uri!r}"}, 500)
        payload = uri
        if s3_key_flag := request.url.query:
            payload += "?" + s3_key_flag
    url = await get_downurl(client, payload, quoted=False, async_=True)
    return RedirectResponse(url, 302)

if __name__ == "__main__":
    from uvicorn import run
    run(app, host="0.0.0.0", port=8123)