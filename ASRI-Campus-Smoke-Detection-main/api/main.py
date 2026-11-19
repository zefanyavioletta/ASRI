"""ASRI API bridge: keeps the existing app.py detection/database functions."""
from __future__ import annotations

import base64
import os
import tempfile
from datetime import datetime, date
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

# Run this module from the existing project root (the folder containing app.py).
import app as legacy
from .auth import install_auth  # login, sesi, role admin/operator

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

app = FastAPI(title="ASRI API", version="1.0.0")


def image_data_url(pil_image: Image.Image) -> str:
    buffer = BytesIO()
    pil_image.save(buffer, format="JPEG", quality=90)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def rows_to_dict(rows):
    return [
        {
            "id": r[0], "time": r[1], "source": r[2], "object_class": r[3],
            "confidence": float(r[4]), "count": int(r[5]), "capture_path": r[6] or ""
        }
        for r in rows
    ]


@app.get("/api/health")
def health():
    return {"status": "ok", "model": os.path.basename(legacy.MODEL_PATH), "database": str(legacy.DB_PATH)}


@app.get("/api/dashboard")
def dashboard():
    con = legacy.get_database_connection()
    try:
        today = date.today().isoformat()
        total, cig, vape = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN object_class='cigarette' THEN detection_count ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN object_class='vape' THEN detection_count ELSE 0 END),0) "
            "FROM detection_history WHERE substr(detection_time,1,10)=?", (today,)
        ).fetchone()
        pending, done = con.execute(
            "SELECT COALESCE(SUM(status='Pending'),0), COALESCE(SUM(status='Done'),0) FROM incident_reports"
        ).fetchone()
        latest = con.execute(
            "SELECT id,detection_time,detection_source,object_class,ROUND(confidence*100,2),detection_count,COALESCE(capture_path,'') "
            "FROM detection_history ORDER BY id DESC LIMIT 6"
        ).fetchall()
        return {"today_total": total, "cigarette": cig, "vape": vape, "pending": pending,
                "done": done, "latest": rows_to_dict(latest)}
    finally:
        con.close()


@app.post("/api/detect/image")
async def detect_image(file: UploadFile = File(...), confidence: float = Form(legacy.DEFAULT_CONFIDENCE)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File harus berupa gambar.")
    if not 0.1 <= confidence <= 0.9:
        raise HTTPException(400, "Confidence harus antara 10% dan 90%.")
    try:
        raw = await file.read()
        pil = Image.open(BytesIO(raw)).convert("RGB")
        output, summary = legacy.detect_image(pil, confidence)
        # Detection rows are read from DB after the existing backend function logs them.
        con = legacy.get_database_connection()
        try:
            latest = con.execute(
                "SELECT id,detection_time,detection_source,object_class,ROUND(confidence*100,2),detection_count,COALESCE(capture_path,'') "
                "FROM detection_history WHERE detection_source='Image' ORDER BY id DESC LIMIT 10"
            ).fetchall()
        finally:
            con.close()
        return {"image": image_data_url(output), "summary": summary, "records": rows_to_dict(latest)}
    except Exception as exc:
        raise HTTPException(500, f"Deteksi gambar gagal: {exc}")


@app.post("/api/detect/camera/frame")
async def camera_frame(file: UploadFile = File(...), confidence: float = Form(legacy.DEFAULT_CONFIDENCE)):
    try:
        pil = Image.open(BytesIO(await file.read())).convert("RGB")
        output, summary, fps, alert, signal = legacy.detect_webcam_stream(pil, confidence, True)
        return {"image": image_data_url(output) if output else None, "summary": summary,
                "fps": fps, "alert": alert, "signal": signal}
    except Exception as exc:
        raise HTTPException(500, f"Pemrosesan frame kamera gagal: {exc}")


@app.post("/api/detect/video")
async def detect_video(file: UploadFile = File(...), confidence: float = Form(legacy.DEFAULT_CONFIDENCE)):
    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name
        output_path, summary = legacy.detect_video(tmp_path, confidence)
        if not output_path:
            raise HTTPException(400, summary)
        return {"video_url": f"/api/video-result?path={base64.urlsafe_b64encode(output_path.encode()).decode()}", "summary": summary}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Deteksi video gagal: {exc}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.get("/api/video-result")
def video_result(path: str):
    try:
        decoded = base64.urlsafe_b64decode(path.encode()).decode()
        resolved = Path(decoded).resolve()
        # Only allow generated temp MP4 outputs, never arbitrary file reads.
        if not resolved.name.endswith("_h264.mp4") or not resolved.is_file():
            raise HTTPException(404, "Video hasil tidak ditemukan.")
        return FileResponse(str(resolved), media_type="video/mp4", filename="asri-detection-result.mp4")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Path video tidak valid.")


@app.get("/api/history")
def history(source: str = "All", object_class: str = "All"):
    rows, total, cigarette, vape = legacy.get_detection_history(source, object_class)
    return {"records": rows_to_dict(rows), "total_records": int(total),
            "cigarette_count": int(cigarette), "vape_count": int(vape)}


@app.get("/api/captures/{record_id}")
def capture(record_id: int):
    con = legacy.get_database_connection()
    try:
        row = con.execute("SELECT capture_path FROM detection_history WHERE id=?", (record_id,)).fetchone()
    finally:
        con.close()
    if not row or not row[0] or not os.path.isfile(row[0]):
        raise HTTPException(404, "Capture tidak tersedia untuk record ini.")
    return FileResponse(row[0], media_type="image/jpeg")


@app.get("/api/incidents")
def incidents(status: str = "All"):
    if status not in ("All", "Pending", "Done"):
        raise HTTPException(400, "Filter status tidak valid.")
    records = legacy.get_incident_reports(status)
    return {"records": [
        {"id": r[0], "detection_id": r[1], "time": r[2], "object_class": r[3],
         "confidence": float(r[4]), "status": r[5], "notes": r[6], "capture_path": r[7]}
        for r in records
    ]}


@app.patch("/api/incidents/{incident_id}")
def update_incident(incident_id: int, status: str = Form(...), notes: str = Form("")):
    result = legacy.update_incident_status(incident_id, status, notes)
    if result.startswith("ERROR"):
        raise HTTPException(400, result)
    return {"message": result}


@app.delete("/api/incidents/{incident_id}")
def delete_false_positive(incident_id: int):
    result = legacy.delete_false_positive(incident_id)
    if result.startswith("ERROR"):
        raise HTTPException(400, result)
    return {"message": result}


@app.get("/api/statistics")
def statistics():
    con = legacy.get_database_connection()
    try:
        by_day = con.execute(
            "SELECT substr(detection_time,1,10) day, SUM(detection_count) total "
            "FROM detection_history GROUP BY day ORDER BY day DESC LIMIT 7"
        ).fetchall()
        by_class = con.execute(
            "SELECT object_class,SUM(detection_count) FROM detection_history GROUP BY object_class"
        ).fetchall()
        total = con.execute("SELECT COALESCE(SUM(detection_count),0) FROM detection_history").fetchone()[0]
        avg = con.execute("SELECT COALESCE(AVG(confidence)*100,0) FROM detection_history").fetchone()[0]
        return {"total": total, "average_confidence": round(avg, 2),
                "by_day": [{"date": r[0], "total": r[1]} for r in reversed(by_day)],
                "by_class": {r[0]: r[1] for r in by_class}}
    finally:
        con.close()


@app.get("/api/config")
def config():
    return {"model": os.path.basename(legacy.MODEL_PATH), "classes": list(legacy.model.names.values()),
            "image_size": legacy.IMAGE_SIZE, "default_confidence": legacy.DEFAULT_CONFIDENCE}


# Autentikasi: semua /api/* (kecuali login/logout) wajib login; akun & hapus insiden khusus admin.
install_auth(app)

# Serve the separated frontend from the same origin as the API.
if FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
