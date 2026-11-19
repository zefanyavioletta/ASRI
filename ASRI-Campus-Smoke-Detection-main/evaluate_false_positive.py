
import os
import csv
import cv2
from ultralytics import YOLO

# ============================================================
# EVALUASI MODEL YOLOv11 - VIDEO POSITIF
# Project: C:\Smoke-Detection
# ============================================================

# 1. KONFIGURASI

MODEL_PATH = os.path.join("models", "model_final.pt")
VIDEO_PATH = "cigarette_test.mp4"

FRAME_INTERVAL = 3
IMAGE_SIZE = 640

THRESHOLDS = [0.25, 0.40, 0.50, 0.65, 0.75]
BASE_CONFIDENCE = 0.05

SUMMARY_PATH = "cigarette_threshold_summary.csv"
DETAILS_PATH = "cigarette_threshold_details.csv"


# 2. VALIDASI FILE

if not os.path.isfile(MODEL_PATH):
    raise FileNotFoundError(
        f"Model tidak ditemukan: {MODEL_PATH}"
    )

if not os.path.isfile(VIDEO_PATH):
    raise FileNotFoundError(
        f"Video tidak ditemukan: {VIDEO_PATH}\n"
        "Pastikan video asli berada di folder utama "
        "dengan nama positive_test.mp4."
    )

if FRAME_INTERVAL < 1:
    raise ValueError("FRAME_INTERVAL minimal 1.")

if not THRESHOLDS or any(
    t <= BASE_CONFIDENCE or t > 1
    for t in THRESHOLDS
):
    raise ValueError("Konfigurasi threshold tidak valid.")


# 3. LOAD MODEL

print("=" * 55)
print("EVALUASI YOLOv11 - VIDEO POSITIF")
print("=" * 55)

print("\nMemuat model...")
model = YOLO(MODEL_PATH)

print("MODEL BERHASIL DIMUAT")
print("Kelas model:", model.names)


# 4. BUKA VIDEO

cap = cv2.VideoCapture(VIDEO_PATH)

if not cap.isOpened():
    raise RuntimeError("Video tidak dapat dibuka.")

fps = cap.get(cv2.CAP_PROP_FPS)
total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

if fps <= 0 or total_frames <= 0:
    cap.release()
    raise RuntimeError("Metadata video tidak valid.")

duration = total_frames / fps

print(f"\nVideo: {VIDEO_PATH}")
print(f"Durasi: {duration:.2f} detik")
print(f"FPS: {fps:.2f}")
print(f"Total frame: {total_frames}")
print(f"Sampling: setiap {FRAME_INTERVAL} frame")
print(f"Threshold: {THRESHOLDS}")

print("\nMemulai proses evaluasi...")


# 5. SIAPKAN STATISTIK

class_names = list(model.names.values())

stats = {
    threshold: {
        class_name: {
            "boxes": 0,
            "frames": set(),
            "max_conf": 0.0
        }
        for class_name in class_names
    }
    for threshold in THRESHOLDS
}

detail_rows = []

frame_index = 0
sampled_frames = 0


# 6. PROSES VIDEO

try:
    while True:

        success, frame = cap.read()

        if not success:
            break

        if frame_index % FRAME_INTERVAL == 0:

            sampled_frames += 1

            results = model.predict(
                source=frame,
                imgsz=IMAGE_SIZE,
                conf=BASE_CONFIDENCE,
                verbose=False
            )

            result = results[0]

            if result.boxes is not None:

                for box in result.boxes:

                    class_id = int(box.cls[0])
                    class_name = model.names[class_id]

                    confidence = float(box.conf[0])

                    x1, y1, x2, y2 = [
                        float(v)
                        for v in box.xyxy[0].tolist()
                    ]

                    for threshold in THRESHOLDS:

                        if confidence >= threshold:

                            item = stats[threshold][class_name]

                            item["boxes"] += 1
                            item["frames"].add(frame_index)

                            item["max_conf"] = max(
                                item["max_conf"],
                                confidence
                            )

                            detail_rows.append({
                                "threshold": threshold,
                                "frame_index": frame_index,
                                "time_seconds": round(
                                    frame_index / fps, 3
                                ),
                                "class": class_name,
                                "confidence": round(
                                    confidence, 6
                                ),
                                "x1": round(x1, 2),
                                "y1": round(y1, 2),
                                "x2": round(x2, 2),
                                "y2": round(y2, 2)
                            })

        frame_index += 1

finally:
    cap.release()


# 7. SIMPAN DETAIL CSV

detail_fields = [
    "threshold",
    "frame_index",
    "time_seconds",
    "class",
    "confidence",
    "x1",
    "y1",
    "x2",
    "y2"
]

with open(
    DETAILS_PATH,
    "w",
    newline="",
    encoding="utf-8-sig"
) as file:

    writer = csv.DictWriter(
        file,
        fieldnames=detail_fields
    )

    writer.writeheader()
    writer.writerows(detail_rows)


# 8. SIMPAN SUMMARY CSV

summary_fields = [
    "threshold",
    "class",
    "total_boxes",
    "frames_with_detection",
    "max_confidence"
]

with open(
    SUMMARY_PATH,
    "w",
    newline="",
    encoding="utf-8-sig"
) as file:

    writer = csv.DictWriter(
        file,
        fieldnames=summary_fields
    )

    writer.writeheader()

    for threshold in THRESHOLDS:

        for class_name, item in stats[threshold].items():

            writer.writerow({
                "threshold": threshold,
                "class": class_name,
                "total_boxes": item["boxes"],
                "frames_with_detection": len(
                    item["frames"]
                ),
                "max_confidence": round(
                    item["max_conf"], 6
                )
            })


# 9. TAMPILKAN HASIL

print("\n" + "=" * 55)
print("HASIL EVALUASI VIDEO POSITIF")
print("=" * 55)

print("Total frame:", total_frames)
print("Frame dianalisis:", sampled_frames)

for threshold in THRESHOLDS:

    print(f"\nTHRESHOLD: {threshold:.2f}")

    for class_name, item in stats[threshold].items():

        print(
            f"  {class_name}: "
            f"{item['boxes']} prediksi, "
            f"{len(item['frames'])} frame, "
            f"confidence maksimum "
            f"{item['max_conf']:.4f}"
        )

print("\nFILE HASIL:")
print("-", SUMMARY_PATH)
print("-", DETAILS_PATH)

print("\nEVALUASI SELESAI.")
print("App.py, model, dan database tidak diubah.")
