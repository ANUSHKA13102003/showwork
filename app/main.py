import json
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import ask, briefing, cleaner, db, llm, scorecard, security
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


@app.get("/api/briefings")
def api_briefings():
    saved = set(briefing.list_saved_weeks())
    eligible = list(reversed(briefing.eligible_weeks()))
    # Show saved briefings first (latest to earliest), followed by remaining historical weeks
    ordered = [w for w in eligible if w in saved] + [w for w in eligible if w not in saved]
    return {"weeks": [{"week": w, "saved": w in saved} for w in ordered]}


@app.get("/api/briefing/{week}")
def api_briefing(week: str):
    try:
        return JSONResponse(briefing.load_or_compute(week))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/clean/sample")
def api_clean_sample():
    return {"filename": "online_retail_messy_sample.csv", "csv": cleaner.build_sample_messy_csv()}


@app.post("/api/clean/inspect", dependencies=[Depends(security.enforce_rate_limit)])
async def api_clean_inspect(file: Annotated[UploadFile, File(...)]):
    name, df = await cleaner.read_validated_csv(file)
    issues = cleaner.detect_issues(df)
    default_ids = {i["id"] for i in issues if i.get("default_accepted", True)}
    preview = cleaner.apply_fixes(df, default_ids, preview_only=True)
    return {
        "filename": name,
        "input_rows": len(df),
        "input_columns": len(df.columns),
        "columns": list(df.columns),
        "issues": issues,
        "preview_before": df.head(8).to_dict(orient="records"),
        "preview": preview,
    }


@app.post("/api/clean/apply", dependencies=[Depends(security.enforce_rate_limit)])
async def api_clean_apply(
    file: Annotated[UploadFile, File(...)],
    accepted_ids: Annotated[str, Form()] = "[]",
    preview_only: Annotated[str, Form()] = "0",
):
    _, df = await cleaner.read_validated_csv(file)
    try:
        parsed_ids = set(json.loads(accepted_ids))
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="accepted_ids must be a JSON array.") from exc
    return cleaner.apply_fixes(df, parsed_ids, preview_only=(preview_only == "1"))


def _index():
    return FileResponse(STATIC / "index.html")


@app.get("/")
def root_page():
    return _index()


@app.get("/briefing")
def briefing_page():
    return _index()


@app.get("/ask")
def ask_page():
    return _index()


@app.get("/clean")
def clean_page():
    return _index()


@app.get("/scorecard")
def scorecard_page():
    return _index()
