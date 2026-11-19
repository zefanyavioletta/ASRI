ASRI FRONTEND — FINAL UI REFINEMENT

Pemasangan:
1. Hentikan server Uvicorn dengan Ctrl+C.
2. Backup folder frontend yang sedang dipakai.
3. Ekstrak ZIP ini.
4. Salin folder frontend dari ZIP ke folder utama project ASRI, menimpa folder frontend lama.
5. Jangan ubah app.py, api/, database, model, captures, atau requirements.
6. Jalankan kembali: uvicorn api.main:api --host 127.0.0.1 --port 8000
7. Buka http://127.0.0.1:8000 lalu tekan Ctrl+F5.

Catatan penting:
- Paket ini hanya mengubah frontend. Endpoint API yang dipakai tetap sama.
- Login/logout belum tersedia karena backend lama belum menyediakan autentikasi dan endpoint akun. UI tidak membuat login palsu yang hanya menyembunyikan halaman di browser.
- Halaman Profil & Pengaturan menampilkan profil administrator lokal dan informasi model. Untuk login multi-user yang aman, backend perlu ditambah autentikasi pada tahap tersendiri.
- Gunakan localhost/HTTPS untuk izin kamera browser.

Perubahan dashboard pada paket ini:
- Gambar dashboard memakai file frontend/assets/campus-situation-animation.jpg. Untuk menggantinya nanti, cukup ganti file tersebut dengan gambar baru memakai nama file yang sama.
- Tanggal header mengikuti tanggal perangkat secara otomatis, dengan zona waktu Asia/Jakarta.
- Waktu riwayat dan incident ditampilkan dalam format Indonesia, misalnya: Minggu, 15 Nov 2026 14:34:19 WIB.
- Sidebar memakai gradasi hijau dan area akun diringkas menjadi Administrator + status Online.

UPDATE — LOGIN & PERAN (admin / operator)
- Login sekarang tersedia (api/auth.py + frontend/js/auth.js). Semua /api/* wajib login, kecuali login/logout.
- Akun admin pertama dibuat otomatis saat server pertama kali jalan. Atur lewat env ASRI_ADMIN_USERNAME,
  ASRI_ADMIN_EMAIL, ASRI_ADMIN_PASSWORD; bila password tidak diisi, password acak dicetak SEKALI di terminal.
- Admin: semua fitur + Tambah Akun + Kelola Akun (ganti password, ubah email, hapus) + hapus incident report.
- Operator: semua fitur pemantauan, tanpa membuat akun dan tanpa menghapus incident report.
- Data akun & sesi disimpan di detection_history.db (tabel users, auth_sessions). Password di-hash (scrypt).
- Tidak ada dependensi baru.
