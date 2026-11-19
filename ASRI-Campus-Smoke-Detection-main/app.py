import gradio as gr
from ultralytics import YOLO
from PIL import Image
import os
import time
import cv2
import tempfile
import subprocess
import imageio_ffmpeg
import sqlite3
import threading
from datetime import datetime, timedelta
import shutil


# =====================================
# 1. PATH DAN LOAD MODEL YOLOv11
# =====================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE_DIR, "models")
DB_PATH = os.path.join(BASE_DIR, "detection_history.db")
CAPTURE_DIR = os.path.join(BASE_DIR, "captures")
WEBCAM_CAPTURE_INTERVAL = 60  # detik

# Nama model deployment E8. model_final.pt disediakan sebagai fallback.
MODEL_CANDIDATES = [
    os.environ.get("YOLO_MODEL_PATH", ""),
    os.path.join(MODELS_DIR, "smoke_detection_yolo11s.pt"),
    os.path.join(MODELS_DIR, "model_final.pt"),
]
MODEL_PATH = next(
    (path for path in MODEL_CANDIDATES if path and os.path.isfile(path)),
    None
)

if MODEL_PATH is None:
    expected = os.path.join(MODELS_DIR, "smoke_detection_yolo11s.pt")
    raise FileNotFoundError(
        "Model YOLO tidak ditemukan. Letakkan smoke_detection_yolo11s.pt "
        f"di folder models. Lokasi yang dicari: {expected}"
    )

# Threshold default dinaikkan menjadi 0.42 sesuai deployment_config.json.
# Ini dapat mengurangi deteksi ber-confidence rendah, tetapi berisiko melewatkan
# objek benar yang confidence-nya di bawah 0.42.
DEFAULT_CONFIDENCE = 0.42
IMAGE_SIZE = 640

print("Memuat model YOLOv11 E8...")
print("MODEL PATH:", MODEL_PATH)
model = YOLO(MODEL_PATH)
print("MODEL BERHASIL DIMUAT")
print("KELAS MODEL:", model.names)

expected_classes = {"cigarette", "vape"}
loaded_classes = set(model.names.values()) if isinstance(model.names, dict) else set(model.names)
if not expected_classes.issubset(loaded_classes):
    raise RuntimeError(
        f"Kelas model tidak sesuai. Diharapkan {expected_classes}, "
        f"ditemukan {loaded_classes}"
    )


# =====================================
# 2. DATABASE SETUP - SQLITE
# =====================================

def get_database_connection():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_database():
    connection = get_database_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS detection_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detection_time TEXT NOT NULL,
                detection_source TEXT NOT NULL,
                object_class TEXT NOT NULL,
                confidence REAL NOT NULL,
                detection_count INTEGER NOT NULL DEFAULT 1,
                capture_path TEXT
            )
            """
        )
        # Migrasi aman untuk database lama: tambahkan kolom jika belum ada.
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(detection_history)").fetchall()}
        if "capture_path" not in columns:
            cursor.execute("ALTER TABLE detection_history ADD COLUMN capture_path TEXT")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS incident_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detection_id INTEGER NOT NULL UNIQUE,
                incident_time TEXT NOT NULL,
                object_class TEXT NOT NULL,
                confidence REAL NOT NULL,
                capture_path TEXT,
                status TEXT NOT NULL DEFAULT 'Pending',
                notes TEXT,
                checked_at TEXT,
                FOREIGN KEY(detection_id) REFERENCES detection_history(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_incident_status ON incident_reports(status)")
        connection.commit()
    finally:
        connection.close()

    print("DATABASE BERHASIL DISIAPKAN")
    print("DATABASE LOCATION:", DB_PATH)


init_database()


# =====================================
# 3. DETECTION LOGGING
# =====================================

def save_capture(image_array, prefix="capture"):
    """Simpan gambar hasil anotasi YOLO ke folder captures."""
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{prefix}_{timestamp}.jpg"
    path = os.path.join(CAPTURE_DIR, filename)
    if isinstance(image_array, Image.Image):
        image_array.save(path, format="JPEG", quality=90)
    else:
        cv2.imwrite(path, image_array, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    return path


def save_detection_records(source, detections, capture_path=None):
    """Simpan rekap per kelas dan buat incident Pending untuk cigarette."""
    if not detections:
        return
    grouped = {}
    for detection in detections:
        class_name = detection["class"]
        score = float(detection["confidence"])
        if class_name not in grouped:
            grouped[class_name] = {"count": 0, "max_confidence": 0.0}
        grouped[class_name]["count"] += 1
        grouped[class_name]["max_confidence"] = max(grouped[class_name]["max_confidence"], score)

    detection_time = datetime.now().astimezone().isoformat(timespec="seconds")
    connection = get_database_connection()
    try:
        cursor = connection.cursor()
        for class_name, data in grouped.items():
            cursor.execute(
                """INSERT INTO detection_history
                (detection_time, detection_source, object_class, confidence, detection_count, capture_path)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (detection_time, source, class_name, data["max_confidence"], data["count"], capture_path)
            )
            detection_id = cursor.lastrowid
            # Alert/incident dibuat untuk cigarette; vape tetap tercatat di history.
            if class_name == "cigarette":
                cursor.execute(
                    """INSERT OR IGNORE INTO incident_reports
                    (detection_id, incident_time, object_class, confidence, capture_path, status)
                    VALUES (?, ?, ?, ?, ?, 'Pending')""",
                    (detection_id, detection_time, class_name, data["max_confidence"], capture_path)
                )
        connection.commit()
        print(f"LOGGING BERHASIL: {source} - {len(grouped)} kelas")
    except sqlite3.Error:
        connection.rollback()
        raise
    finally:
        connection.close()


# =====================================
# 4. WEBCAM LOGGING COOLDOWN
# =====================================

webcam_last_logged = {}
webcam_log_lock = threading.Lock()
WEBCAM_LOG_COOLDOWN = 10
webcam_last_capture = 0.0
webcam_capture_lock = threading.Lock()


# =====================================
# 4A. WEBCAM ALERT CONFIGURATION
# =====================================

# Jeda notifikasi browser: 15 detik
BROWSER_ALERT_COOLDOWN = 15

browser_alert_last_sent = 0.0
browser_alert_lock = threading.Lock()


def create_webcam_alert(detections):
    """
    Membuat website alert dan browser notification
    ketika objek cigarette terdeteksi.
    """

    global browser_alert_last_sent

    cigarette_detected = any(
        item["class"] == "cigarette"
        for item in detections
    )

    if not cigarette_detected:
        return "✅ Tidak ada cigarette terdeteksi.", ""

    website_alert = (
        "🚨 **PERINGATAN! OBJEK CIGARETTE TERDETEKSI!**\n\n"
        "Sistem mendeteksi objek cigarette melalui webcam."
    )

    browser_signal = ""
    current_time = time.monotonic()

    with browser_alert_lock:

        if current_time - browser_alert_last_sent >= BROWSER_ALERT_COOLDOWN:

            browser_alert_last_sent = current_time

            browser_signal = f"cigarette:{time.time_ns()}"

    return website_alert, browser_signal


def log_webcam_detections(detections, capture_path=None):
    """Catat tiap kelas dari webcam maksimal sekali per 10 detik."""

    if not detections:
        return

    current_time = time.monotonic()
    grouped = {}

    for detection in detections:
        class_name = detection["class"]
        grouped.setdefault(class_name, []).append(detection)

    records_to_save = []

    with webcam_log_lock:
        for class_name, items in grouped.items():
            last_time = webcam_last_logged.get(class_name, 0)

            if current_time - last_time >= WEBCAM_LOG_COOLDOWN:
                records_to_save.extend(items)
                webcam_last_logged[class_name] = current_time

    if records_to_save:
        save_detection_records("Webcam", records_to_save, capture_path)


# =====================================
# ANNOTASI BOUNDING BOX DENGAN CONFIDENCE PERSEN
# =====================================

def annotate_result(result):
    """Gambar bounding box YOLO dengan label confidence dalam persen.

    Keluaran berupa array BGR agar kompatibel dengan OpenCV/video writer.
    """
    image = result.orig_img.copy()
    if result.boxes is None:
        return image

    height, width = image.shape[:2]
    thickness = max(2, int(round(min(height, width) / 400)))
    font_scale = max(0.5, min(height, width) / 1000)

    for box in result.boxes:
        x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
        class_id = int(box.cls[0])
        class_name = model.names[class_id]
        confidence_percent = float(box.conf[0]) * 100
        label = f"{class_name} {confidence_percent:.2f}%"

        color = (0, 200, 0)  # hijau dalam format BGR
        cv2.rectangle(image, (x1, y1), (x2, y2), color, thickness)
        (text_width, text_height), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
        )
        label_top = max(0, y1 - text_height - baseline - 6)
        cv2.rectangle(
            image,
            (x1, label_top),
            (x1 + text_width + 8, y1),
            color,
            -1
        )
        cv2.putText(
            image,
            label,
            (x1 + 4, y1 - baseline - 3),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA
        )

    return image


# =====================================
# 5. FUNGSI DETEKSI GAMBAR
# =====================================

def detect_image(image, confidence):

    if image is None:
        return None, "Silakan upload gambar terlebih dahulu."

    results = model.predict(
        source=image,
        imgsz=IMAGE_SIZE,
        conf=confidence,
        verbose=False
    )

    result = results[0]
    annotated = annotate_result(result)
    annotated = annotated[:, :, ::-1]
    output_image = Image.fromarray(annotated)

    detections = []
    detection_records = []

    for box in result.boxes:
        class_id = int(box.cls[0])
        class_name = model.names[class_id]
        score = float(box.conf[0])

        detections.append(
            f"- {class_name}: {score * 100:.2f}%"
        )

        detection_records.append({
            "class": class_name,
            "confidence": score
        })

    capture_path = save_capture(annotated[:, :, ::-1], "image") if detection_records else None
    save_detection_records("Image", detection_records, capture_path)

    if not detections:
        summary = "Tidak ditemukan objek cigarette atau vape."
    else:
        summary = (
            f"Ditemukan {len(detections)} objek.\n\n"
            + "\n".join(detections)
        )

    return output_image, summary


# =====================================
# 6. FUNGSI REAL-TIME WEBCAM
# =====================================


def detect_webcam_stream(image, confidence, camera_active):

    if not camera_active:
        return (
            None,
            "Monitoring dihentikan.",
            "0.00",
            "Monitoring tidak aktif.",
            ""
        )

    if image is None:
        return (
            None,
            "Menunggu frame kamera...",
            "0.00",
            "Menunggu frame kamera...",
            ""
        )

    start_time = time.perf_counter()

    results = model.predict(
        source=image,
        imgsz=IMAGE_SIZE,
        conf=confidence,
        verbose=False
    )

    result = results[0]
    annotated = annotate_result(result)
    annotated = annotated[:, :, ::-1]
    output_image = Image.fromarray(annotated)

    detections = []
    detection_records = []

    for box in result.boxes:
        class_id = int(box.cls[0])
        class_name = model.names[class_id]
        score = float(box.conf[0])

        detections.append(
            f"- {class_name}: {score * 100:.2f}%"
        )

        detection_records.append({
            "class": class_name,
            "confidence": score
        })

    # MEMBUAT WEBSITE ALERT DAN BROWSER SIGNAL
    alert_message, browser_signal = create_webcam_alert(
        detection_records
    )

    # Capture pertama saat ada deteksi, lalu maksimal sekali tiap 60 detik.
    capture_path = None
    global webcam_last_capture
    if detection_records:
        now = time.monotonic()
        with webcam_capture_lock:
            if now - webcam_last_capture >= WEBCAM_CAPTURE_INTERVAL:
                capture_path = save_capture(annotated[:, :, ::-1], "webcam")
                webcam_last_capture = now
    # Log webcam mengikuti cooldown; capture disertakan jika interval capture tercapai.
    log_webcam_detections(detection_records, capture_path)

    elapsed_time = time.perf_counter() - start_time

    inference_fps = (
        1.0 / elapsed_time if elapsed_time > 0 else 0.0
    )

    if not detections:
        summary = "Tidak ditemukan objek cigarette atau vape."
    else:
        summary = (
            f"Terdeteksi {len(detections)} objek:\n\n"
            + "\n".join(detections)
        )

    return (
        output_image,
        summary,
        f"{inference_fps:.2f}",
        alert_message,
        browser_signal
    )


# =====================================
# 7. FUNGSI START DAN STOP MONITORING
# =====================================

def start_camera():
    return True, "MONITORING AKTIF"


def stop_camera():
    return False, "MONITORING DIHENTIKAN"


# =====================================
# 8. FUNGSI VIDEO DETECTION
# =====================================

def detect_video(video_path, confidence):

    if video_path is None:
        return None, "Silakan upload video terlebih dahulu."

    cap = cv2.VideoCapture(video_path)

    if not cap.isOpened():
        return None, "Video tidak dapat dibuka."

    original_fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if original_fps <= 0 or total_frames <= 0:
        cap.release()
        return None, "Metadata video tidak valid."

    frame_interval = 3

    original_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    original_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if original_width <= 0 or original_height <= 0:
        cap.release()
        return None, "Resolusi video tidak valid."

    scale = min(
        640 / original_width,
        640 / original_height
    )

    output_width = max(2, int(original_width * scale) // 2 * 2)
    output_height = max(2, int(original_height * scale) // 2 * 2)

    temp_file = tempfile.NamedTemporaryFile(
        suffix=".mp4",
        delete=False
    )
    output_path = temp_file.name
    temp_file.close()

    final_path = output_path.replace(".mp4", "_h264.mp4")

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(
        output_path,
        fourcc,
        original_fps,
        (output_width, output_height)
    )

    if not writer.isOpened():
        cap.release()
        if os.path.exists(output_path):
            os.remove(output_path)
        return None, "Gagal membuat video sementara."

    processed_frames = 0
    total_detections = 0
    class_counts = {
        "cigarette": 0,
        "vape": 0
    }

    video_detection_records = []
    representative_frames = {}
    last_annotated = None
    frame_index = 0

    try:
        while True:
            ret, frame = cap.read()

            if not ret:
                break

            resized = cv2.resize(
                frame,
                (output_width, output_height)
            )

            if frame_index % frame_interval == 0:
                results = model.predict(
                    source=resized,
                    imgsz=IMAGE_SIZE,
                    conf=confidence,
                    verbose=False
                )

                result = results[0]
                last_annotated = annotate_result(result)
                processed_frames += 1

                for box in result.boxes:
                    class_id = int(box.cls[0])
                    class_name = model.names[class_id]
                    score = float(box.conf[0])

                    total_detections += 1

                    if class_name in class_counts:
                        class_counts[class_name] += 1

                    video_detection_records.append({
                        "class": class_name,
                        "confidence": score
                    })
                    if (class_name not in representative_frames or
                            score > representative_frames[class_name]["confidence"]):
                        representative_frames[class_name] = {
                            "confidence": score,
                            "image": annotate_result(result)
                        }

            if last_annotated is not None:
                writer.write(last_annotated)
            else:
                writer.write(resized)

            frame_index += 1

    except Exception:
        cap.release()
        writer.release()

        for path in [output_path, final_path]:
            if os.path.exists(path):
                os.remove(path)
        raise

    finally:
        cap.release()
        writer.release()

    # Convert video ke format H.264 agar kompatibel dengan browser
    try:
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

        command = [
            ffmpeg_path,
            "-y",
            "-i", output_path,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-an",
            final_path
        ]

        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True
        )

        if not os.path.exists(final_path):
            raise RuntimeError("File H.264 tidak berhasil dibuat.")

        if os.path.getsize(final_path) == 0:
            raise RuntimeError("File H.264 kosong.")

    except Exception as e:
        for path in [output_path, final_path]:
            if os.path.exists(path):
                os.remove(path)
        return None, f"Gagal mengonversi video: {str(e)}"

    if os.path.exists(output_path):
        os.remove(output_path)

    # Simpan maksimal satu capture terbaik per kelas, bukan setiap frame.
    for class_name, item in representative_frames.items():
        capture_path = save_capture(item["image"], f"video_{class_name}")
        class_records = [d for d in video_detection_records if d["class"] == class_name]
        save_detection_records("Video", class_records, capture_path)

    summary = (
        f"VIDEO DETECTION SELESAI\n\n"
        f"Total frame video: {total_frames}\n"
        f"Frame yang diproses YOLO: {processed_frames}\n"
        f"Frame sampling: setiap {frame_interval} frame\n\n"
        f"Total temuan objek: {total_detections}\n\n"
        f"Cigarette: {class_counts['cigarette']}\n"
        f"Vape: {class_counts['vape']}\n\n"
        f"Output format: MP4 (H.264)"
    )

    return final_path, summary




# =====================================
# 9. DETECTION HISTORY - DATABASE QUERY
# =====================================

def get_detection_history(source_filter="All", class_filter="All"):
    """Ambil riwayat deteksi dan ringkasan dari SQLite."""

    query = """
        SELECT id, detection_time, detection_source, object_class,
               ROUND(confidence * 100, 2), detection_count, COALESCE(capture_path, '')
        FROM detection_history
        WHERE 1 = 1
    """
    params = []

    if source_filter != "All":
        query += " AND detection_source = ?"
        params.append(source_filter)

    if class_filter != "All":
        query += " AND object_class = ?"
        params.append(class_filter)

    query += " ORDER BY id DESC LIMIT 500"

    connection = get_database_connection()
    try:
        cursor = connection.cursor()
        rows = cursor.execute(query, params).fetchall()

        summary_query = """
            SELECT COUNT(*),
                COALESCE(SUM(CASE WHEN object_class = 'cigarette'
                    THEN detection_count ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN object_class = 'vape'
                    THEN detection_count ELSE 0 END), 0)
            FROM detection_history
            WHERE 1 = 1
        """
        summary_params = []

        if source_filter != "All":
            summary_query += " AND detection_source = ?"
            summary_params.append(source_filter)

        if class_filter != "All":
            summary_query += " AND object_class = ?"
            summary_params.append(class_filter)

        summary = cursor.execute(
            summary_query, summary_params
        ).fetchone()

        return rows, str(summary[0]), str(summary[1]), str(summary[2])
    finally:
        connection.close()


def refresh_history(source_filter, class_filter):
    return get_detection_history(source_filter, class_filter)


def update_detection_record_ui(
    record_id,
    detection_source,
    object_class,
    confidence_percent,
    detection_count,
    source_filter,
    class_filter
):
    status = update_detection_record(
        record_id,
        detection_source,
        object_class,
        confidence_percent,
        detection_count
    )

    history = refresh_history(source_filter, class_filter)

    return (status, *history)

# =====================================
# 9A. UPDATE DETECTION RECORD
# =====================================

def update_detection_record(
    record_id,
    detection_source,
    object_class,
    confidence_percent,
    detection_count
):
    """Memperbarui data riwayat deteksi berdasarkan ID."""

    try:
        record_id = int(record_id)
        confidence_percent = float(confidence_percent)
        detection_count = int(detection_count)

    except (ValueError, TypeError):
        return "ERROR: Pastikan semua input diisi dengan benar."

    # Validasi data
    if record_id <= 0:
        return "ERROR: ID record harus lebih dari 0."

    if detection_source not in ["Image", "Webcam", "Video"]:
        return "ERROR: Detection source tidak valid."

    if object_class not in ["cigarette", "vape"]:
        return "ERROR: Object class tidak valid."

    if not 0 <= confidence_percent <= 100:
        return "ERROR: Confidence harus antara 0 sampai 100."

    if detection_count <= 0:
        return "ERROR: Detection count harus lebih dari 0."

    connection = get_database_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            UPDATE detection_history
            SET detection_source = ?,
                object_class = ?,
                confidence = ?,
                detection_count = ?
            WHERE id = ?
            """,
            (
                detection_source,
                object_class,
                confidence_percent / 100,
                detection_count,
                record_id
            )
        )

        if cursor.rowcount == 0:
            return f"ERROR: Record dengan ID {record_id} tidak ditemukan."

        connection.commit()

        return f"BERHASIL: Record ID {record_id} berhasil diperbarui."

    except sqlite3.Error as e:
        connection.rollback()
        return f"ERROR DATABASE: {str(e)}"

    finally:
        connection.close()


# =====================================
# 9B. DELETE DETECTION RECORD
# =====================================

def delete_detection_record(record_id):
    """Menghapus satu record, incident terkait, dan capture-nya."""
    try:
        record_id = int(record_id)
    except (ValueError, TypeError):
        return "ERROR: ID record harus berupa angka."
    if record_id <= 0:
        return "ERROR: ID record harus lebih dari 0."
    connection = get_database_connection()
    try:
        cursor = connection.cursor()
        row = cursor.execute("SELECT capture_path FROM detection_history WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return f"ERROR: Record dengan ID {record_id} tidak ditemukan."
        capture_path = row[0]
        cursor.execute("DELETE FROM incident_reports WHERE detection_id = ?", (record_id,))
        cursor.execute("DELETE FROM detection_history WHERE id = ?", (record_id,))
        connection.commit()
        if capture_path and os.path.isfile(capture_path):
            try: os.remove(capture_path)
            except OSError: pass
        return f"BERHASIL: Record ID {record_id}, incident terkait, dan capture dihapus."
    except sqlite3.Error as e:
        connection.rollback()
        return f"ERROR DATABASE: {str(e)}"
    finally:
        connection.close()



def delete_detection_record_ui(
    record_id, confirmation, source_filter, class_filter
):
    """Hapus satu record setelah pengguna memberi konfirmasi."""

    if not confirmation:
        status = "PENGHAPUSAN DIBATALKAN: Centang konfirmasi terlebih dahulu."
    else:
        status = delete_detection_record(record_id)

    history = refresh_history(source_filter, class_filter)
    return (status, *history, False)


# =====================================
# 9C. CLEAR DETECTION HISTORY (TASK 3H)
# =====================================

def clear_detection_history():
    """Menghapus seluruh laporan, history, dan file capture terkait."""
    connection = get_database_connection()
    try:
        cursor = connection.cursor()
        paths = [r[0] for r in cursor.execute("SELECT capture_path FROM detection_history WHERE capture_path IS NOT NULL").fetchall()]
        count = cursor.execute("SELECT COUNT(*) FROM detection_history").fetchone()[0]
        cursor.execute("DELETE FROM incident_reports")
        cursor.execute("DELETE FROM detection_history")
        connection.commit()
        for path in paths:
            if path and os.path.isfile(path):
                try: os.remove(path)
                except OSError: pass
        return f"BERHASIL: Seluruh riwayat dan incident dihapus ({count} record)."
    except sqlite3.Error as e:
        connection.rollback()
        return f"ERROR DATABASE: {str(e)}"
    finally:
        connection.close()


def clear_detection_history_ui(confirmation, source_filter, class_filter):
    """Hapus seluruh riwayat hanya jika pengguna mengonfirmasi."""

    if not confirmation:
        status = (
            "PENGHAPUSAN DIBATALKAN: Centang konfirmasi terlebih dahulu."
        )
    else:
        status = clear_detection_history()

    history = refresh_history(source_filter, class_filter)
    return (status, *history, False)


# =====================================
# 9D. DATABASE VALIDATION (TASK 3I)
# =====================================

def validate_database():
    con = get_database_connection()
    try:
        cur = con.cursor()
        total = cur.execute("SELECT COUNT(*) FROM detection_history").fetchone()[0]
        invalid_class = cur.execute("SELECT COUNT(*) FROM detection_history WHERE object_class NOT IN ('cigarette','vape')").fetchone()[0]
        invalid_source = cur.execute("SELECT COUNT(*) FROM detection_history WHERE detection_source NOT IN ('Image','Webcam','Video')").fetchone()[0]
        invalid_conf = cur.execute("SELECT COUNT(*) FROM detection_history WHERE confidence < 0 OR confidence > 1").fetchone()[0]
        invalid_count = cur.execute("SELECT COUNT(*) FROM detection_history WHERE detection_count <= 0").fetchone()[0]
        missing_capture = sum(1 for (path,) in cur.execute("SELECT capture_path FROM detection_history WHERE capture_path IS NOT NULL").fetchall() if not os.path.isfile(path))
        orphan_incident = cur.execute("SELECT COUNT(*) FROM incident_reports ir LEFT JOIN detection_history dh ON dh.id = ir.detection_id WHERE dh.id IS NULL").fetchone()[0]
        integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
        return (f"DATABASE VALIDATION\nTotal detection records: {total}\nInvalid object class: {invalid_class}\nInvalid source: {invalid_source}\nInvalid confidence: {invalid_conf}\nInvalid detection count: {invalid_count}\nMissing capture files: {missing_capture}\nOrphan incident reports: {orphan_incident}\nSQLite integrity: {integrity}\n\n" + ("VALIDATION BERHASIL: tidak ditemukan masalah." if integrity == "ok" and not any([invalid_class, invalid_source, invalid_conf, invalid_count, missing_capture, orphan_incident]) else "PERLU PEMERIKSAAN: ditemukan data yang tidak sesuai."))
    finally:
        con.close()


# =====================================
# 9E. DATABASE BACKUP & RECOVERY (TASK 3J)
# =====================================

def create_database_backup():
    backup_dir = os.path.join(BASE_DIR, "database_backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, f"detection_history_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
    source = sqlite3.connect(DB_PATH, timeout=30)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup_path, f"BACKUP BERHASIL: {backup_path}"


def restore_database_backup(uploaded_path):
    if not uploaded_path or not os.path.isfile(uploaded_path):
        return "ERROR: Pilih file backup database (.db) terlebih dahulu."
    try:
        check = sqlite3.connect(uploaded_path)
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required = {"detection_history", "incident_reports"}
        if not required.issubset(tables):
            check.close()
            return "ERROR: File backup tidak memiliki struktur database aplikasi yang diperlukan."
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            check.close()
            return "ERROR: Backup rusak dan tidak dapat dipulihkan."
        target = sqlite3.connect(DB_PATH, timeout=30)
        try:
            check.backup(target)
        finally:
            target.close()
            check.close()
        return "RESTORE BERHASIL: Database berhasil dipulihkan dari backup. Refresh halaman untuk melihat data."
    except sqlite3.Error as e:
        return f"ERROR RESTORE: {str(e)}"


# =====================================
# 9D. INCIDENT REPORTS (TASK 3L)
# =====================================

def get_incident_reports(status_filter="All"):
    query = """SELECT ir.id, ir.detection_id, ir.incident_time, ir.object_class,
               ROUND(ir.confidence * 100, 2), ir.status, COALESCE(ir.notes, ''),
               COALESCE(ir.capture_path, '')
               FROM incident_reports ir"""
    params = []
    if status_filter != "All":
        query += " WHERE ir.status = ?"
        params.append(status_filter)
    query += " ORDER BY ir.id DESC LIMIT 500"
    con = get_database_connection()
    try:
        return con.execute(query, params).fetchall()
    finally:
        con.close()


def update_incident_status(incident_id, status, notes):
    try:
        incident_id = int(incident_id)
    except (ValueError, TypeError):
        return "ERROR: Incident ID harus berupa angka."
    if status not in ["Pending", "Done"]:
        return "ERROR: Status tidak valid."
    con = get_database_connection()
    try:
        cur = con.cursor()
        cur.execute("UPDATE incident_reports SET status = ?, notes = ?, checked_at = ? WHERE id = ?",
                    (status, (notes or "").strip(), datetime.now().astimezone().isoformat(timespec="seconds") if status == "Done" else None, incident_id))
        if cur.rowcount == 0:
            return f"ERROR: Incident {incident_id} tidak ditemukan."
        con.commit()
        return f"BERHASIL: Incident {incident_id} diperbarui menjadi {status}."
    except sqlite3.Error as e:
        con.rollback()
        return f"ERROR DATABASE: {str(e)}"
    finally:
        con.close()


def delete_false_positive(incident_id):
    """Hapus incident, detection history terkait, dan capture sekaligus."""
    try:
        incident_id = int(incident_id)
    except (ValueError, TypeError):
        return "ERROR: Incident ID harus berupa angka."
    con = get_database_connection()
    try:
        cur = con.cursor()
        row = cur.execute("SELECT detection_id, capture_path FROM incident_reports WHERE id = ?", (incident_id,)).fetchone()
        if row is None:
            return f"ERROR: Incident {incident_id} tidak ditemukan."
        detection_id, capture_path = row
        cur.execute("DELETE FROM incident_reports WHERE id = ?", (incident_id,))
        cur.execute("DELETE FROM detection_history WHERE id = ?", (detection_id,))
        con.commit()
        if capture_path and os.path.isfile(capture_path):
            try: os.remove(capture_path)
            except OSError: pass
        return f"BERHASIL: Incident {incident_id}, history ID {detection_id}, dan capture dihapus."
    except sqlite3.Error as e:
        con.rollback()
        return f"ERROR DATABASE: {str(e)}"
    finally:
        con.close()


def incident_refresh(status_filter):
    return get_incident_reports(status_filter)


def show_capture(record_id):
    try: record_id = int(record_id)
    except (ValueError, TypeError): return None, "Masukkan ID record yang valid."
    con = get_database_connection()
    try:
        row = con.execute("SELECT capture_path FROM detection_history WHERE id = ?", (record_id,)).fetchone()
    finally:
        con.close()
    if not row or not row[0] or not os.path.isfile(row[0]):
        return None, "Capture tidak tersedia untuk record ini."
    return row[0], f"Capture untuk record ID {record_id}."


# =====================================
# 10. TAMPILAN WEBSITE
# =====================================

with gr.Blocks(title="Smoke Detection System") as app:

    gr.Markdown(
        """
        # SMOKE DETECTION MONITORING SYSTEM

        ### YOLOv11 Object Detection

        Sistem pendeteksi objek cigarette dan vape
        berbasis Computer Vision.
        """
    )

    # IMAGE DETECTION
    gr.Markdown("## Image Detection")

    with gr.Row():
        input_image = gr.Image(
            label="Upload Gambar",
            type="pil",
            sources=["upload"]
        )

        output_image = gr.Image(
            label="Hasil Deteksi"
        )

    confidence = gr.Slider(
        minimum=0.1,
        maximum=0.9,
        value=DEFAULT_CONFIDENCE,
        step=0.01,
        label="Confidence Threshold (default 42%)"
    )

    detect_button = gr.Button(
        "Mulai Deteksi",
        variant="primary"
    )

    output_text = gr.Textbox(
        label="Detection Result",
        lines=5
    )

    detect_button.click(
        fn=detect_image,
        inputs=[input_image, confidence],
        outputs=[output_image, output_text]
    )

    gr.Markdown("---")

    # REAL-TIME WEBCAM DETECTION
    gr.Markdown("## Real-Time Webcam Detection")

    camera_active = gr.State(False)

    camera_status = gr.Textbox(
        label="Camera Status",
        value="MONITORING TIDAK AKTIF",
        interactive=False
    )

    webcam_input = gr.Image(
        label="Live Camera Preview",
        sources=["webcam"],
        type="pil",
        streaming=True
    )

    with gr.Row():
        start_button = gr.Button(
            "Start Camera",
            variant="primary"
        )

        stop_button = gr.Button(
            "Stop Camera",
            variant="stop"
        )

    webcam_output = gr.Image(
        label="Real-Time Detection Result"
    )

    webcam_text = gr.Textbox(
        label="Detection Result",
        lines=5
    )

    webcam_fps = gr.Textbox(
        label="Inference FPS",
        value="0.00",
        interactive=False
    )

    # =====================================
    # WEBSITE ALERT
    # =====================================

    webcam_alert = gr.Markdown(
        value="✅ Sistem monitoring siap.",
        label="Detection Alert"
    )

    # Sinyal tersembunyi untuk memicu Browser Notification
    browser_signal = gr.Textbox(
        label="Browser Alert Signal (Testing)",
        value="",
        visible=False
    )

    browser_permission_button = gr.Button(
        "Aktifkan Browser Notification",
        variant="secondary"
    )

    start_button.click(
        fn=start_camera,
        inputs=None,
        outputs=[camera_active, camera_status]
    )

    stop_button.click(
        fn=stop_camera,
        inputs=None,
        outputs=[camera_active, camera_status]
    )

    # Meminta izin notifikasi melalui tindakan langsung pengguna
    browser_permission_button.click(
        fn=None,
        inputs=None,
        outputs=None,
        js="""
        () => {
            if (!("Notification" in window)) {
                alert("Browser ini tidak mendukung notifikasi.");
                return;
            }

            Notification.requestPermission().then(permission => {
                if (permission === "granted") {
                    alert("Izin Browser Notification berhasil diaktifkan.");
                } else {
                    alert("Izin notifikasi tidak diberikan. Periksa pengaturan browser.");
                }
            });
        }
        """
    )


    # =====================================
    # BROWSER NOTIFICATION EVENT
    # =====================================

    browser_signal.change(
        fn=None,
        inputs=[browser_signal],
        outputs=None,
        js="""
        (signal) => {

            if (!signal || !signal.startsWith("cigarette:")) {
                return;
            }

            console.log("BROWSER ALERT SIGNAL DITERIMA:", signal);

            if (!("Notification" in window)) {
                alert("Browser tidak mendukung Notification API.");
                return;
            }

            if (Notification.permission !== "granted") {
                alert("Izin Browser Notification belum diberikan.");
                return;
            }

            const notification = new Notification(
                "PERINGATAN! CIGARETTE TERDETEKSI",
                {
                    body: "Sistem mendeteksi objek cigarette melalui webcam.",
                    tag: "cigarette-alert",
                    requireInteraction: true
                }
            );

            console.log("NOTIFIKASI BERHASIL DIBUAT");

        }
        """
    )

    webcam_input.stream(
        fn=detect_webcam_stream,
        inputs=[
            webcam_input,
            confidence,
            camera_active
        ],
        outputs=[
            webcam_output,
            webcam_text,
            webcam_fps,
            webcam_alert,
            browser_signal
        ],
        stream_every=0.2,
        concurrency_limit=1
    )

    # VIDEO DETECTION
    gr.Markdown("---")
    gr.Markdown("## Video Detection")

    gr.Markdown(
        """
        Upload video untuk mendeteksi objek
        cigarette dan vape menggunakan YOLOv11.
        """
    )

    video_input = gr.Video(
        label="Upload Video",
        sources=["upload"]
    )

    video_confidence = gr.Slider(
        minimum=0.1,
        maximum=0.9,
        value=DEFAULT_CONFIDENCE,
        step=0.01,
        label="Video Confidence Threshold (default 42%)"
    )

    video_button = gr.Button(
        "Mulai Video Detection",
        variant="primary"
    )

    video_output = gr.Video(
        label="Processed Video"
    )

    video_summary = gr.Textbox(
        label="Video Detection Summary",
        lines=8
    )

    video_button.click(
        fn=detect_video,
        inputs=[
            video_input,
            video_confidence
        ],
        outputs=[
            video_output,
            video_summary
        ]
    )

    # ---------------------------------
    # DETECTION HISTORY
    # ---------------------------------

    gr.Markdown("---")
    gr.Markdown("## Detection History")
    gr.Markdown(
        "Riwayat hasil deteksi yang tersimpan secara permanen "
        "di database SQLite."
    )

    with gr.Row():
        history_source_filter = gr.Dropdown(
            choices=["All", "Image", "Webcam", "Video"],
            value="All",
            label="Filter Detection Source"
        )
        history_class_filter = gr.Dropdown(
            choices=["All", "cigarette", "vape"],
            value="All",
            label="Filter Object Class"
        )

    with gr.Row():
        history_total_records = gr.Textbox(
            label="Total Records", value="0", interactive=False
        )
        history_cigarette_count = gr.Textbox(
            label="Cigarette Count", value="0", interactive=False
        )
        history_vape_count = gr.Textbox(
            label="Vape Count", value="0", interactive=False
        )

    history_table = gr.Dataframe(
        headers=[
            "ID", "Detection Time", "Source", "Object Class",
            "Confidence (%)", "Detection Count", "Capture Path"
        ],
        datatype=["number", "str", "str", "str", "number", "number", "str"],
        value=[],
        interactive=False,
        wrap=True,
        label="Detection Records"
    )
    with gr.Row():
        capture_record_id = gr.Number(label="Record ID untuk melihat capture", value=1, precision=0)
        show_capture_button = gr.Button("Lihat Bukti Capture")
    capture_preview = gr.Image(label="Bukti Capture dengan Bounding Box", type="filepath")
    capture_preview_status = gr.Textbox(label="Capture Status", interactive=False)
    show_capture_button.click(fn=show_capture, inputs=[capture_record_id], outputs=[capture_preview, capture_preview_status])

    # =====================================
    # UPDATE DETECTION RECORD FORM
    # =====================================

    with gr.Accordion("Update Detection Record", open=False):

        update_record_id = gr.Number(
            label="Record ID",
            value=1,
            precision=0
        )

        update_source = gr.Dropdown(
            choices=["Image", "Webcam", "Video"],
            value="Image",
            label="Detection Source"
        )

        update_class = gr.Dropdown(
            choices=["cigarette", "vape"],
            value="cigarette",
            label="Object Class"
        )

        update_confidence = gr.Slider(
            minimum=0,
            maximum=100,
            value=80,
            step=0.01,
            label="Confidence (%)"
        )

        update_count = gr.Number(
            label="Detection Count",
            value=1,
            precision=0
        )

        update_button = gr.Button(
            "Update Record",
            variant="primary"
        )

        update_status = gr.Textbox(
            label="Update Status",
            interactive=False
        )

    # =====================================
    # DELETE DETECTION RECORD FORM (TASK 3G)
    # =====================================

    with gr.Accordion("Delete Detection Record", open=False):
        gr.Markdown(
            "Hapus satu baris riwayat berdasarkan ID. "
            "Tindakan ini tidak dapat dibatalkan."
        )

        delete_record_id = gr.Number(
            label="Record ID yang akan dihapus",
            value=1,
            precision=0
        )

        delete_confirmation = gr.Checkbox(
            label="Saya yakin ingin menghapus record ini",
            value=False
        )

        delete_button = gr.Button(
            "Delete Record",
            variant="stop"
        )

        delete_status = gr.Textbox(
            label="Delete Status",
            interactive=False
        )

    # =====================================
    # CLEAR DETECTION HISTORY FORM (TASK 3H)
    # =====================================

    with gr.Accordion("Clear Detection History", open=False):
        gr.Markdown(
            "**PERINGATAN:** Fitur ini menghapus seluruh riwayat deteksi "
            "(Image, Webcam, dan Video). Tindakan ini tidak dapat dibatalkan."
        )

        clear_confirmation = gr.Checkbox(
            label="Saya memahami dan yakin ingin menghapus SELURUH riwayat deteksi",
            value=False
        )

        clear_history_button = gr.Button(
            "Clear All Detection History",
            variant="stop"
        )

        clear_history_status = gr.Textbox(
            label="Clear History Status",
            interactive=False
        )

    # =====================================
    # DATABASE VALIDATION & BACKUP (TASK 3I-3J)
    # =====================================
    gr.Markdown("---")
    gr.Markdown("## Database Maintenance")
    with gr.Accordion("Database Validation", open=False):
        validate_button = gr.Button("Validate Database")
        validation_output = gr.Textbox(label="Validation Result", lines=9, interactive=False)
        validate_button.click(fn=validate_database, inputs=[], outputs=[validation_output])
    with gr.Accordion("Database Backup & Recovery", open=False):
        backup_button = gr.Button("Create Database Backup", variant="primary")
        backup_file_output = gr.File(label="Download Database Backup")
        backup_status = gr.Textbox(label="Backup Status", interactive=False)
        backup_button.click(fn=create_database_backup, inputs=[], outputs=[backup_file_output, backup_status])
        gr.Markdown("Restore akan mengganti isi database aktif dengan isi file backup yang dipilih.")
        restore_file_input = gr.File(label="Pilih file backup (.db)", file_types=[".db"], type="filepath")
        restore_button = gr.Button("Restore Database", variant="stop")
        restore_status = gr.Textbox(label="Restore Status", interactive=False)
        restore_button.click(fn=restore_database_backup, inputs=[restore_file_input], outputs=[restore_status])

    # =====================================
    # INCIDENT REPORTS (TASK 3L)
    # =====================================
    gr.Markdown("---")
    gr.Markdown("## Incident Reports")
    gr.Markdown("Laporan cigarette yang perlu diverifikasi dan ditindaklanjuti security.")
    with gr.Row():
        incident_filter = gr.Dropdown(choices=["All", "Pending", "Done"], value="All", label="Filter Incident Status")
        incident_refresh_button = gr.Button("Refresh Incident Reports")
    incident_table = gr.Dataframe(
        headers=["Incident ID", "Detection ID", "Waktu", "Object", "Confidence (%)", "Status", "Catatan", "Capture Path"],
        datatype=["number", "number", "str", "str", "number", "str", "str", "str"],
        value=[], interactive=False, wrap=True, label="Incident Queue"
    )
    with gr.Row():
        incident_capture_detection_id = gr.Number(label="Detection ID untuk melihat bukti incident", value=1, precision=0)
        incident_capture_button = gr.Button("Lihat Bukti Incident")
    incident_capture_preview = gr.Image(label="Bukti Incident", type="filepath")
    incident_capture_status = gr.Textbox(label="Capture Status", interactive=False)
    incident_capture_button.click(fn=show_capture, inputs=[incident_capture_detection_id], outputs=[incident_capture_preview, incident_capture_status])
    with gr.Accordion("Review / Update Incident", open=False):
        incident_id_input = gr.Number(label="Incident ID", precision=0, value=1)
        incident_status_input = gr.Dropdown(choices=["Pending", "Done"], value="Done", label="Status")
        incident_notes_input = gr.Textbox(label="Catatan pemeriksaan / tindakan security", lines=3)
        incident_update_button = gr.Button("Simpan Tindak Lanjut", variant="primary")
        incident_update_status = gr.Textbox(label="Update Status", interactive=False)
    with gr.Accordion("Hapus False Positive", open=False):
        gr.Markdown("Penghapusan ini menghapus incident report, detection history terkait, dan file capture buktinya sekaligus.")
        false_positive_id = gr.Number(label="Incident ID", precision=0, value=1)
        false_positive_confirm = gr.Checkbox(label="Saya sudah memeriksa dan yakin ini false positive", value=False)
        false_positive_button = gr.Button("Hapus False Positive", variant="stop")
        false_positive_status = gr.Textbox(label="Deletion Status", interactive=False)

    history_refresh_button = gr.Button(
        "Refresh History", variant="primary"
    )

    history_outputs = [
        history_table,
        history_total_records,
        history_cigarette_count,
        history_vape_count
    ]
    history_inputs = [
        history_source_filter,
        history_class_filter
    ]


    update_button.click(
        fn=update_detection_record_ui,
        inputs=[
            update_record_id,
            update_source,
            update_class,
            update_confidence,
            update_count,
            history_source_filter,
            history_class_filter
        ],
        outputs=[
            update_status,
            *history_outputs
        ]
    )

    delete_button.click(
        fn=delete_detection_record_ui,
        inputs=[
            delete_record_id,
            delete_confirmation,
            history_source_filter,
            history_class_filter
        ],
        outputs=[
            delete_status,
            *history_outputs,
            delete_confirmation
        ]
    )

    clear_history_button.click(
        fn=clear_detection_history_ui,
        inputs=[
            clear_confirmation,
            history_source_filter,
            history_class_filter
        ],
        outputs=[
            clear_history_status,
            *history_outputs,
            clear_confirmation
        ]
    )

    incident_refresh_button.click(fn=incident_refresh, inputs=[incident_filter], outputs=[incident_table])
    incident_filter.change(fn=incident_refresh, inputs=[incident_filter], outputs=[incident_table])

    def update_incident_ui(incident_id, status, notes, status_filter):
        message = update_incident_status(incident_id, status, notes)
        return message, incident_refresh(status_filter)

    def delete_false_positive_ui(incident_id, confirmation, status_filter):
        if not confirmation:
            message = "PENGHAPUSAN DIBATALKAN: centang konfirmasi terlebih dahulu."
        else:
            message = delete_false_positive(incident_id)
        return message, incident_refresh(status_filter), False

    incident_update_button.click(fn=update_incident_ui,
        inputs=[incident_id_input, incident_status_input, incident_notes_input, incident_filter],
        outputs=[incident_update_status, incident_table])
    false_positive_button.click(fn=delete_false_positive_ui,
        inputs=[false_positive_id, false_positive_confirm, incident_filter],
        outputs=[false_positive_status, incident_table, false_positive_confirm])

    history_refresh_button.click(
        fn=refresh_history,
        inputs=history_inputs,
        outputs=history_outputs
    )

    history_source_filter.change(
        fn=refresh_history,
        inputs=history_inputs,
        outputs=history_outputs
    )

    history_class_filter.change(
        fn=refresh_history,
        inputs=history_inputs,
        outputs=history_outputs
    )

    app.load(
        fn=refresh_history,
        inputs=history_inputs,
        outputs=history_outputs
    )

    app.load(fn=incident_refresh, inputs=[incident_filter], outputs=[incident_table])

    # FOOTER
    gr.Markdown(
        """
        ---
        **Powered by YOLOv11**

        Cigarette Detection | Vape Detection
        """
    )


# =====================================
# 11. RUN APPLICATION
# =====================================

if __name__ == "__main__":
    print("Menjalankan Smoke Detection System...")
    print("Buka browser: http://127.0.0.1:7860")

    app.launch(
        server_name="127.0.0.1",
        server_port=7860,
        show_error=True
    )
