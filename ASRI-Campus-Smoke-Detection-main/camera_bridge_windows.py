"""ASRI CCTV Camera Bridge for Windows 11.
Reads one RTSP camera locally and sends sampled frames to ASRI Railway.
Run from a Windows PC that can reach the CCTV on its local network.
"""
import argparse
import time
import cv2
import requests


def main():
    parser = argparse.ArgumentParser(description="ASRI CCTV Camera Bridge")
    parser.add_argument("--url", required=True, help="ASRI URL, e.g. https://asri-production.up.railway.app")
    parser.add_argument("--camera-id", required=True, type=int, help="Camera ID shown after adding CCTV in ASRI")
    parser.add_argument("--token", required=True, help="Secret camera token shown by ASRI")
    parser.add_argument("--rtsp", required=True, help="CCTV RTSP URL")
    parser.add_argument("--interval", type=float, default=1.5, help="Seconds between frames (default 1.5)")
    args = parser.parse_args()
    endpoint = args.url.rstrip("/") + f"/api/cameras/{args.camera_id}/frame"
    cap = cv2.VideoCapture(args.rtsp, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise SystemExit("Tidak dapat membuka stream CCTV. Periksa RTSP URL dan jaringan CCTV.")
    print("ASRI CCTV Bridge berjalan. Tekan Ctrl+C untuk berhenti.")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("Stream terputus; mencoba menyambungkan kembali...")
                cap.release()
                time.sleep(3)
                cap = cv2.VideoCapture(args.rtsp, cv2.CAP_FFMPEG)
                continue
            height, width = frame.shape[:2]
            if width > 960:
                frame = cv2.resize(frame, (960, int(height * 960 / width)))
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 72])
            if ok:
                try:
                    response = requests.post(endpoint, data={"token": args.token, "confidence": "0.42"},
                                             files={"file": ("cctv-frame.jpg", encoded.tobytes(), "image/jpeg")}, timeout=30)
                    response.raise_for_status()
                    print("Frame terkirim ke ASRI", time.strftime("%H:%M:%S"))
                except requests.RequestException as exc:
                    print("Gagal mengirim frame:", exc)
            time.sleep(max(0.5, args.interval))
    except KeyboardInterrupt:
        print("Bridge dihentikan.")
    finally:
        cap.release()


if __name__ == "__main__":
    main()
