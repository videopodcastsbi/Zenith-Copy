# Zenith Copy

Tempel link sosial media → muncul tombol download (video, gambar, audio).

**Mesin:** [yt-dlp](https://github.com/yt-dlp/yt-dlp) (video, 1000+ situs) + [gallery-dl](https://github.com/mikf/gallery-dl) (gambar/carousel: IG, Pinterest, X, dll).

## Menjalankan di komputer sendiri

Butuh: **Python 3.10+** dan **ffmpeg** (untuk menggabung video+audio dan konversi MP3).

```bash
pip install -r requirements.txt
python main.py
```

Buka `http://localhost:8000`.

## Deploy online (supaya bisa dipakai dari HP / dibagikan)

Ada `Dockerfile`, jadi tinggal upload ke Railway / Render / Fly.io / VPS:

```bash
docker build -t zenith-copy .
docker run -p 8000:8000 -e ZENITH_SECRET=isi_bebas_acak zenith-copy
```

Set `ZENITH_SECRET` ke string acak tetap, supaya tombol download gambar tidak kedaluwarsa saat server restart.

## Endpoint API (bisa dipakai juga dari app Flutter)

| Method | Path | Fungsi |
|---|---|---|
| POST | `/api/info` `{"url": "..."}` | Ambil judul, thumbnail, daftar item dan pilihan download |
| GET | `/api/download?url=&fmt=v720&item=0` | Download via yt-dlp (`fmt`: `v{tinggi}`, `vbest`, `audio`) |
| GET | `/api/file?src=&sig=&ext=&name=` | Proxy file langsung (gambar/video CDN), URL harus ditandatangani server |
| GET | `/api/health` | Cek versi yt-dlp & ffmpeg |

## Tips

- **IG / Facebook minta login?** Export cookies dari browser (ekstensi "Get cookies.txt LOCALLY"), simpan sebagai `cookies.txt`, lalu jalankan dengan `COOKIES_FILE=cookies.txt`.
- **Situs tiba-tiba gagal?** Platform sering ganti sistem. Update mesinnya: `pip install -U yt-dlp gallery-dl`.
- **YouTube sering memblokir IP server cloud.** Kalau itu terjadi, pakai cookies atau jalankan di jaringan rumah.
- Keamanan: link ke IP lokal/privat ditolak (anti-SSRF), dan proxy file hanya menerima URL yang ditandatangani server, jadi tidak bisa dipakai sebagai open proxy.
