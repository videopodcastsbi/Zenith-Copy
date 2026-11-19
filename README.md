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

### Environment variable

| Nama | Default | Fungsi |
|---|---|---|
| `ZENITH_SECRET` | acak tiap start | Kunci tanda tangan link proxy file (**wajib di-set** saat hosting) |
| `TRUST_PROXY` | `0` | Set `1` kalau di belakang proxy (Railway/Render/Fly) supaya rate limit pakai IP asli pengunjung |
| `MAX_FILESIZE_MB` | `500` | Batas ukuran 1 file |
| `MAX_DURATION_SEC` | `7200` | Batas durasi video (detik) |
| `DOWNLOAD_TIMEOUT_SEC` | `600` | Batas waktu 1 proses download |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Jumlah download yt-dlp yang boleh jalan bersamaan |
| `MAX_CONCURRENT_INFO` | `5` | Jumlah pengambilan info yang boleh jalan bersamaan |
| `ALLOW_GENERIC` | `0` | Set `1` untuk mengaktifkan extractor "generic" yt-dlp (situs tak dikenal). Dimatikan default demi keamanan (anti-SSRF) |
| `COOKIES_FILE` | – | Path cookies.txt (opsional) |

Rate limit per IP: info 20/menit, download 8/menit, file 60/menit.

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
