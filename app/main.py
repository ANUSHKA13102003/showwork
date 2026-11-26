from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import ask, db, llm, scorecard, security
from app.config import STATIC

app = FastAPI(title="Showwork", docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    return security.add_security_headers(response)


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class AskPayload(BaseModel):
    question: str


@app.get("/health")
def health():
    m = db.meta()
    return {
        "status": "ok",
        "rows": m.get("rows", 0),
        "as_of": m.get("as_of"),
        "mode": llm.mode(),
    }


@app.get("/api/status")
def api_status():
    m = db.meta()
    return {
        "mode": llm.mode(),
        "as_of": m.get("as_of"),
        "min_date": m.get("min_date"),
        "max_date": m.get("max_date"),
        "rows": m.get("rows", 0),
        "suggested_questions": ask.SUGGESTED_QUESTIONS,
    }


@app.post("/api/ask", dependencies=[Depends(security.enforce_rate_limit)])
def api_ask(payload: AskPayload):
    q = security.validate_question(payload.question)
    return JSONResponse(ask.answer(q, use_llm=True))


@app.get("/api/scorecard")
def api_scorecard():
    return JSONResponse(scorecard.load_scorecard())


def _index():
    return FileResponse(STATIC / "index.html")


@app.get("/")
def root_page():
    return _index()


@app.get("/ask")
def ask_page():
    return _index()


@app.get("/scorecard")
def scorecard_page():
    return _index()
