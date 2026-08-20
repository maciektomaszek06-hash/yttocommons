import json
import os
import shutil
import subprocess
import sys
import platform
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
import yt_dlp

DEFAULT_SERVER = "https://YOUR-TOOL.toolforge.org"
CONFIG_DIR = Path.home() / ".yttocommons-agent"
CONFIG_FILE = CONFIG_DIR / "config.json"

PLAYER_CLIENTS = ["default", "mweb", "android", "web"]

PROTOCOL_NAME = "yttocommons-agent"


def get_ffmpeg_path():
    """
    Return the bundled FFmpeg executable path when running as a PyInstaller EXE.
    In source mode, fall back to ffmpeg from PATH.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates = [
            exe_dir / "ffmpeg.exe",
            Path(getattr(sys, "_MEIPASS", exe_dir)) / "ffmpeg.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    return shutil.which("ffmpeg") or "ffmpeg"


def ensure_url_protocol_registered():
    """
    On Windows, register the custom URL protocol automatically on first launch.
    This removes the need for a separate BAT file or manual setup.
    """
    if platform.system().lower() != "windows":
        return

    cfg = load_config()
    if cfg.get("url_protocol_registered"):
        return

    try:
        install_url_protocol()
        cfg = load_config()
        cfg["url_protocol_registered"] = True
        save_config(cfg)
    except Exception as exc:
        print("Warning: could not register URL protocol automatically:", exc)


def install_url_protocol():
    """
    Register yttocommons-agent:// links on Windows so the website can launch
    the local agent after a local job is created.

    Works best when agent.py is packaged as agent.exe. For source usage, it
    registers the current Python interpreter + this script.
    """
    if platform.system().lower() != "windows":
        print("Automatic protocol registration is currently supported only on Windows.")
        return

    try:
        import winreg
    except ImportError:
        print("Windows registry module is unavailable.")
        return

    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}" "%1"'
    else:
        script = os.path.abspath(__file__)
        command = f'"{sys.executable}" "{script}" "%1"'

    root_path = fr"Software\Classes\{PROTOCOL_NAME}"

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root_path) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:YouTubeToCommons Agent")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")

    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER,
        root_path + r"\shell\open\command"
    ) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)

    print("Protocol installed successfully: yttocommons-agent://")
    return True


def parse_protocol_url(value):
    if not value or not value.startswith(f"{PROTOCOL_NAME}://"):
        return None

    parsed = urlparse(value)
    params = parse_qs(parsed.query)

    return {
        "action": parsed.netloc or parsed.path.strip("/"),
        "server": (params.get("server") or [None])[0],
        "pairing_code": (params.get("pairing_code") or [None])[0],
    }


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def safe_title(text):
    result = "".join(c for c in (text or "media") if c.isalnum() or c in " _-").strip()
    return "_".join(result.split())[:180] or "media"


def extract_with_fallback(url, extra_opts, download):
    last = None
    for client in PLAYER_CLIENTS:
        opts = {
        'ffmpeg_location': get_ffmpeg_path(),
            "noplaylist": True,
            "quiet": False,
        }
        opts.update(extra_opts)
        if client != "default":
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
                if info:
                    return info, client
        except Exception as exc:
            last = exc
    raise last or RuntimeError("yt-dlp failed")


def build_commons_text(info, url, media_type, filename, timestamp=None):
    upload_date = info.get("upload_date", "") or ""
    license_tag = "{{YouTube CC-BY-4.0}}" if upload_date >= "20250801" else "{{YouTube CC-BY}}"

    if len(upload_date) == 8:
        date_text = f"{upload_date[:4]}.{upload_date[4:6]}.{upload_date[6:]}"
    else:
        date_text = ""

    author = info.get("uploader", "Unknown")
    clean = os.path.splitext(filename)[0]

    if media_type == "frame":
        seconds = float(timestamp)
        mm, ss = divmod(int(seconds), 60)
        label = f"{mm}:{ss:02d}"
        source = (
            f'[https://www.youtube.com/watch?v={info["id"]}'
            f'&t={int(seconds)}s {label}]'
        )
        top = f'Frame at {label} from Youtube video "{clean}"'
    elif media_type == "audio":
        source = url
        top = f'Audio from Youtube video "{clean}"'
    elif media_type == "thumbnail":
        source = url
        top = f'Thumbnail from Youtube video "{clean}"'
    else:
        source = url
        top = f'Video from Youtube video "{clean}"'

    description = (
        "== {{int:filedesc}} ==\n"
        "{{Information\n"
        f"|description={top}\n"
        f"|date={date_text}\n"
        f"|source={source}\n"
        f"|author={author}\n"
        "}}\n"
        "== {{int:license-header}} ==\n"
        f"{license_tag}\n"
        "{{LicenseReview}}"
        "[[Category:Uploaded with Youtube to Wikimedia Commons]]"
    )

    label = {
        "video": "film",
        "audio": "audio",
        "thumbnail": "thumbnail",
        "frame": "frame",
    }[media_type]

    comment = f"Uploaded a {label} by {author} from {url} with YouTube to Wikimedia Commons"
    return description, comment


def find_video_stream(info):
    if info.get("url") and info.get("vcodec") not in (None, "none"):
        return info

    formats = [
        f for f in info.get("formats", [])
        if f.get("url") and f.get("vcodec") not in (None, "none")
    ]
    if not formats:
        return None

    combined = [f for f in formats if f.get("acodec") not in (None, "none")]
    pool = combined or formats
    return sorted(
        pool,
        key=lambda f: (
            -(f.get("height") or 0),
            -(f.get("tbr") or 0),
        )
    )[0]


def process_job(job):
    url = job["url"]
    media_type = job["type"]
    timestamp = job.get("timestamp")
    workdir = Path(tempfile.mkdtemp(prefix="yttocommons_agent_"))
    old = Path.cwd()
    os.chdir(workdir)

    try:
        if media_type == "video":
            info, client = extract_with_fallback(
                url,
                {
                    "format": "bv*+ba/b/best",
                    "postprocessors": [{
                        "key": "FFmpegVideoConvertor",
                        "preferedformat": "webm",
                    }],
                    "outtmpl": "%(id)s.%(ext)s",
                },
                True,
            )
            path = workdir / f"{info['id']}.webm"

        elif media_type == "audio":
            info, client = extract_with_fallback(
                url,
                {
                    "format": "ba*/b/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "vorbis",
                    }],
                    "outtmpl": "%(id)s.%(ext)s",
                },
                True,
            )
            path = workdir / f"{info['id']}.ogg"

        elif media_type == "thumbnail":
            info, client = extract_with_fallback(
                url,
                {
                    "skip_download": True,
                    "writethumbnail": True,
                    "outtmpl": "%(id)s",
                    "ignore_no_formats_error": True,
                },
                True,
            )
            pics = [
                p for p in workdir.iterdir()
                if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
            if not pics:
                raise RuntimeError("Thumbnail was not downloaded.")
            path = pics[0]

        elif media_type == "frame":
            info, client = extract_with_fallback(
                url,
                {
                    "format": "b/bv*/best",
                    "skip_download": True,
                },
                False,
            )

            selected = find_video_stream(info)
            if not selected:
                raise RuntimeError("No playable video stream found.")

            title = safe_title(info.get("title"))
            path = workdir / f"{title}_{str(timestamp).replace('.', '_')}.jpg"

            headers = dict(info.get("http_headers") or {})
            headers.update(selected.get("http_headers") or {})
            user_agent = headers.pop("User-Agent", None)
            referer = headers.pop("Referer", None)

            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(timestamp)]
            if user_agent:
                cmd += ["-user_agent", user_agent]
            if referer:
                cmd += ["-referer", referer]
            if headers:
                header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                cmd += ["-headers", header_blob]

            cmd += [
                "-i", selected["url"],
                "-frames:v", "1",
                "-q:v", "2",
                str(path),
                "-y",
            ]

            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode != 0 or not path.exists():
                raise RuntimeError(
                    "Local FFmpeg could not capture the frame directly. "
                    f"{result.stderr[-1500:]}"
                )

        else:
            raise RuntimeError("Unknown media type.")

        if not path.exists():
            candidates = [p for p in workdir.iterdir() if p.is_file()]
            if not candidates:
                raise RuntimeError("Output file not found.")
            path = max(candidates, key=lambda p: p.stat().st_size)

        title = safe_title(info.get("title", "media"))

        if media_type == "video":
            filename = f"{title}_(video).webm"
        elif media_type == "audio":
            filename = f"{title}_(audio).ogg"
        elif media_type == "thumbnail":
            filename = f"{title}_(thumbnail){path.suffix.lower()}"
        else:
            seconds = float(timestamp)
            mm, ss = divmod(int(seconds), 60)
            filename = f"{title}_(frame_{mm}-{ss:02d}).jpg"

        final = workdir / filename
        if path != final:
            if final.exists():
                final.unlink()
            path.replace(final)

        description, comment = build_commons_text(
            info, url, media_type, filename, timestamp
        )

        return final, filename, description, comment, workdir, client
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    finally:
        os.chdir(old)


def pair(server, pairing_code=None):
    code = (pairing_code or "").strip()
    if not code:
        code = input("Pairing code from the website: ").strip()

    response = requests.post(
        server.rstrip("/") + "/api/agent/pair",
        json={"pairing_code": code},
        timeout=30,
    )
    response.raise_for_status()
    token = response.json()["agent_token"]

    cfg = {
        "server": server.rstrip("/"),
        "agent_token": token,
    }
    save_config(cfg)
    print("Paired successfully.")
    return cfg


def run(protocol_data=None):
    ensure_url_protocol_registered()
    cfg = load_config()
    protocol_data = protocol_data or {}

    # UWAGA: Tutaj jest zhardkodowany adres Twojego serwera, 
    # tak jak wczesniej prosiles, aby ominac pytanie uzytkownika!
    server = (
        protocol_data.get("server")
        or cfg.get("server")
        or "https://yttocommons.toolforge.org"
    )

    pairing_code = protocol_data.get("pairing_code")

    if pairing_code or not cfg.get("agent_token") or cfg.get("server") != server.rstrip("/"):
        cfg = pair(server, pairing_code=pairing_code)

    token = cfg["agent_token"]
    server = cfg["server"]

    print("Agent running. Press Ctrl+C to stop.")

    while True:
        try:
            response = requests.get(
                server + "/api/agent/jobs/next",
                headers={"Authorization": f"Bearer {token}"},
                timeout=35,
            )

            if response.status_code == 204:
                time.sleep(2)
                continue

            response.raise_for_status()
            job = response.json()
            print(f"Processing {job['id']} - {job['type']}")

            try:
                path, filename, description, comment, workdir, client = process_job(job)

                with open(path, "rb") as handle:
                    result = requests.post(
                        server + f"/api/agent/jobs/{job['id']}/result",
                        headers={"Authorization": f"Bearer {token}"},
                        files={"file": (filename, handle)},
                        data={
                            "title": filename,
                            "description": description,
                            "comment": comment,
                        },
                        timeout=600,
                    )

                result.raise_for_status()
                print("Done:", result.json().get("url"))
                shutil.rmtree(workdir, ignore_errors=True)

            except Exception as exc:
                print("Job error:", exc)
                try:
                    requests.post(
                        server + f"/api/agent/jobs/{job['id']}/error",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"error": str(exc)},
                        timeout=30,
                    )
                except Exception:
                    pass

        except requests.exceptions.HTTPError as exc:
            # NOWY BLOK OBRONNY PRZED BŁĘDEM 401
            if exc.response is not None and exc.response.status_code == 401:
                print("\n" + "="*50)
                print("[!] BŁĄD: Twoja sesja parowania wygasła lub jest nieprawidłowa.")
                print("[!] Usuwam zapamiętany klucz sesji z komputera...")
                if CONFIG_FILE.exists():
                    CONFIG_FILE.unlink()
                print("[!] Wygeneruj nowy kod klikając 'Pair local agent' na stronie.")
                print("="*50 + "\n")
                
                try:
                    cfg = pair(server)
                    token = cfg["agent_token"]
                    print("\nZnów połączono! Czekam na zadania...")
                except Exception as e:
                    print("Nie udało się ponownie sparować:", e)
                    time.sleep(5)
            else:
                print("Agent HTTP error:", exc)
                time.sleep(5)

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as exc:
            print("Agent connection error:", exc)
            time.sleep(5)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--install-protocol":
        install_url_protocol()
    else:
        protocol_data = None
        if len(sys.argv) >= 2:
            protocol_data = parse_protocol_url(sys.argv[1])
        run(protocol_data)