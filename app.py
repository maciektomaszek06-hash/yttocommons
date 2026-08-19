import os
import time
import traceback
import subprocess
import socket
import ipaddress
import sqlite3
import secrets
import json
import tempfile
import uuid
from urllib.parse import urlparse
import requests
import yt_dlp
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'strong_random_session_secret_for_local_use')

API_URL = "https://commons.wikimedia.org/w/api.php"

WIKI_ACCESS_TOKEN = os.environ.get('WIKI_ACCESS_TOKEN')

if not WIKI_ACCESS_TOKEN:
    print("!!! UWAGA: Brak WIKI_ACCESS_TOKEN. Ustaw zmienną środowiskową,")
    print("!!! inaczej upload do Commons nie zadziała.")
else:
    print("DOTS:", WIKI_ACCESS_TOKEN.count('.'))
    print("LEN:", len(WIKI_ACCESS_TOKEN))

_SOURCE_COOKIES_FILE = os.environ.get('YOUTUBE_COOKIES_FILE', 'youtube_cookies.txt')
COOKIES_FILE = _SOURCE_COOKIES_FILE

if os.path.exists(_SOURCE_COOKIES_FILE):
    try:
        writable_path = os.path.join('/tmp', 'youtube_cookies.txt')
        if os.path.abspath(_SOURCE_COOKIES_FILE) != os.path.abspath(writable_path):
            import shutil
            shutil.copyfile(_SOURCE_COOKIES_FILE, writable_path)
            COOKIES_FILE = writable_path
            print(f"[cookies] Skopiowano {_SOURCE_COOKIES_FILE} -> {writable_path} (zapisywalna kopia)")
    except Exception as e:
        print(f"[cookies] UWAGA: nie udało się skopiować cookies do /tmp: {e}")
        print("[cookies] Będę próbował czytać bezpośrednio z oryginalnej ścieżki (może być read-only).")


PLAYER_CLIENTS = ['mweb', 'android', 'web']


AGENT_DB_PATH = os.environ.get(
    'AGENT_DB_PATH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent_jobs.sqlite3')
)


def _db():
    conn = sqlite3.connect(AGENT_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _init_agent_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pairings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pairing_code TEXT UNIQUE NOT NULL,
                browser_token TEXT UNIQUE NOT NULL,
                agent_token TEXT UNIQUE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                browser_token TEXT NOT NULL,
                url TEXT NOT NULL,
                media_type TEXT NOT NULL,
                timestamp TEXT,
                status TEXT NOT NULL,
                error TEXT,
                commons_url TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)


_init_agent_db()


def _bearer_token():
    value = request.headers.get('Authorization', '')
    if value.lower().startswith('bearer '):
        return value[7:].strip()
    return None


def _paired_browser_for_agent(agent_token):
    if not agent_token:
        return None
    now = int(time.time())
    with _db() as conn:
        row = conn.execute(
            """
            SELECT browser_token
            FROM pairings
            WHERE agent_token = ? AND expires_at > ?
            """,
            (agent_token, now)
        ).fetchone()
    return row['browser_token'] if row else None


def _browser_is_paired(browser_token):
    if not browser_token:
        return False
    now = int(time.time())
    with _db() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM pairings
            WHERE browser_token = ?
              AND agent_token IS NOT NULL
              AND expires_at > ?
            """,
            (browser_token, now)
        ).fetchone()
    return bool(row)


@app.route('/')
def index():
    return render_template('index.html', logged_in=bool(WIKI_ACCESS_TOKEN))


@app.route('/check', methods=['POST'])
def check_license():
    url = request.json.get('url')
    if not url:
        return jsonify({'is_cc': False, 'error': 'Missing URL.'})

    try:
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'noplaylist': True,
            'ignore_no_formats_error': True,
            'check_formats': False,
        }
        if os.path.exists(COOKIES_FILE):
            ydl_opts['cookiefile'] = COOKIES_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return jsonify({'is_cc': False, 'error': 'Failed to extract video info.'})

            license_info = info.get('license', '')
            description = info.get('description', '')

            is_creative_commons = False
            if license_info and ('Creative Commons' in license_info or 'Attribution' in license_info):
                is_creative_commons = True
            elif 'Creative Commons' in description or 'CC BY' in description:
                is_creative_commons = True

            if is_creative_commons:
                return jsonify({'is_cc': True, 'title': info.get('title'), 'id': info.get('id')})

            if not license_info:
                return jsonify({'is_cc': False, 'error': 'Brak licencji Creative Commons (standardowa licencja YouTube nie jest dozwolona na Commons).'})

            return jsonify({'is_cc': False, 'error': f'Invalid license: {license_info}'})

    except Exception as e:
        return jsonify({'is_cc': False, 'error': str(e)})


@app.route('/upload', methods=['POST'])
def handle_upload():
    if not WIKI_ACCESS_TOKEN:
        return jsonify({'error': 'Brak skonfigurowanego WIKI_ACCESS_TOKEN na serwerze.'}), 401

    yt_url = request.form.get('url')
    media_type = request.form.get('type')
    timestamp = request.form.get('timestamp')
    download_mode = (request.form.get('download_mode') or 'proxy').strip().lower()
    user_proxy = request.form.get('proxy', '').strip() or None

    if not yt_url or media_type not in ['video', 'audio', 'thumbnail', 'frame']:
        return jsonify({'error': 'Missing required data.'}), 400

    if download_mode == 'local':
        return jsonify({
            'error': 'Local mode must be started through /api/local/jobs and requires the desktop agent.'
        }), 400

    if download_mode != 'proxy':
        return jsonify({'error': 'Invalid download mode.'}), 400

    if not user_proxy:
        return jsonify({'error': 'Proxy mode requires a proxy URL.'}), 400

    try:
        downloaded_file, safe_title, description, comment = download_media(
            yt_url,
            media_type,
            timestamp,
            user_proxy=user_proxy
        )
        commons_url = upload_to_commons(
            downloaded_file,
            safe_title,
            description,
            comment
        )
        if os.path.exists(downloaded_file):
            os.remove(downloaded_file)
        return jsonify({'success': True, 'url': commons_url})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/local/pair/start', methods=['POST'])
def local_pair_start():
    now = int(time.time())
    pairing_code = f"{secrets.randbelow(1000000):06d}"
    browser_token = secrets.token_urlsafe(32)
    expires_at = now + 86400

    with _db() as conn:
        conn.execute(
            """
            INSERT INTO pairings
                (pairing_code, browser_token, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (pairing_code, browser_token, now, expires_at)
        )

    return jsonify({
        'pairing_code': pairing_code,
        'browser_token': browser_token,
        'expires_in': 86400
    })


@app.route('/api/local/pair/status', methods=['GET'])
def local_pair_status():
    browser_token = request.headers.get('X-Browser-Token', '').strip()
    return jsonify({'paired': _browser_is_paired(browser_token)})


@app.route('/api/agent/pair', methods=['POST'])
def agent_pair():
    data = request.get_json(silent=True) or {}
    pairing_code = str(data.get('pairing_code', '')).strip()

    if not pairing_code:
        return jsonify({'error': 'Missing pairing code.'}), 400

    now = int(time.time())
    with _db() as conn:
        row = conn.execute(
            """
            SELECT id
            FROM pairings
            WHERE pairing_code = ?
              AND agent_token IS NULL
              AND expires_at > ?
            """,
            (pairing_code, now)
        ).fetchone()

        if not row:
            return jsonify({'error': 'Invalid or expired pairing code.'}), 404

        agent_token = secrets.token_urlsafe(48)
        conn.execute(
            "UPDATE pairings SET agent_token = ? WHERE id = ?",
            (agent_token, row['id'])
        )

    return jsonify({'agent_token': agent_token})


@app.route('/api/local/jobs', methods=['POST'])
def local_create_job():
    if not WIKI_ACCESS_TOKEN:
        return jsonify({'error': 'Brak skonfigurowanego WIKI_ACCESS_TOKEN na serwerze.'}), 401

    browser_token = request.headers.get('X-Browser-Token', '').strip()
    if not _browser_is_paired(browser_token):
        return jsonify({'error': 'Local agent is not paired.'}), 401

    data = request.get_json(silent=True) or {}
    url = str(data.get('url', '')).strip()
    media_type = str(data.get('type', '')).strip()
    timestamp = data.get('timestamp')

    if not url or media_type not in ['video', 'audio', 'thumbnail', 'frame']:
        return jsonify({'error': 'Missing required data.'}), 400

    if media_type == 'frame' and timestamp in (None, ''):
        return jsonify({'error': 'Timestamp is required for frame.'}), 400

    now = int(time.time())
    job_id = uuid.uuid4().hex

    with _db() as conn:
        conn.execute(
            """
            INSERT INTO jobs
                (id, browser_token, url, media_type, timestamp,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'waiting', ?, ?)
            """,
            (job_id, browser_token, url, media_type,
             None if timestamp is None else str(timestamp), now, now)
        )

    return jsonify({'job_id': job_id, 'status': 'waiting'})


@app.route('/api/local/jobs/<job_id>', methods=['GET'])
def local_job_status(job_id):
    browser_token = request.headers.get('X-Browser-Token', '').strip()

    with _db() as conn:
        row = conn.execute(
            """
            SELECT id, status, error, commons_url
            FROM jobs
            WHERE id = ? AND browser_token = ?
            """,
            (job_id, browser_token)
        ).fetchone()

    if not row:
        return jsonify({'error': 'Job not found.'}), 404

    return jsonify(dict(row))


@app.route('/api/agent/jobs/next', methods=['GET'])
def agent_next_job():
    agent_token = _bearer_token()
    browser_token = _paired_browser_for_agent(agent_token)

    if not browser_token:
        return jsonify({'error': 'Invalid agent token.'}), 401

    now = int(time.time())

    with _db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            """
            SELECT id, url, media_type, timestamp
            FROM jobs
            WHERE browser_token = ? AND status = 'waiting'
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (browser_token,)
        ).fetchone()

        if not row:
            conn.commit()
            return ('', 204)

        conn.execute(
            """
            UPDATE jobs
            SET status = 'processing', updated_at = ?
            WHERE id = ?
            """,
            (now, row['id'])
        )
        conn.commit()

    return jsonify({
        'id': row['id'],
        'url': row['url'],
        'type': row['media_type'],
        'timestamp': row['timestamp']
    })


@app.route('/api/agent/jobs/<job_id>/result', methods=['POST'])
def agent_job_result(job_id):
    agent_token = _bearer_token()
    browser_token = _paired_browser_for_agent(agent_token)

    if not browser_token:
        return jsonify({'error': 'Invalid agent token.'}), 401

    with _db() as conn:
        job = conn.execute(
            """
            SELECT id
            FROM jobs
            WHERE id = ? AND browser_token = ? AND status = 'processing'
            """,
            (job_id, browser_token)
        ).fetchone()

    if not job:
        return jsonify({'error': 'Job not found or not processing.'}), 404

    uploaded = request.files.get('file')
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '')
    comment = request.form.get('comment', '')

    if not uploaded or not title:
        return jsonify({'error': 'Missing file or title.'}), 400

    suffix = os.path.splitext(title)[1]
    fd, temp_path = tempfile.mkstemp(prefix='yttocommons_agent_', suffix=suffix)
    os.close(fd)

    try:
        uploaded.save(temp_path)
        commons_url = upload_to_commons(
            temp_path,
            title,
            description,
            comment
        )

        now = int(time.time())
        with _db() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'done',
                    commons_url = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (commons_url, now, job_id)
            )

        return jsonify({'success': True, 'url': commons_url})
    except Exception as e:
        traceback.print_exc()
        now = int(time.time())
        with _db() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'error',
                    error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (str(e), now, job_id)
            )
        return jsonify({'error': str(e)}), 500
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


@app.route('/api/agent/jobs/<job_id>/error', methods=['POST'])
def agent_job_error(job_id):
    agent_token = _bearer_token()
    browser_token = _paired_browser_for_agent(agent_token)

    if not browser_token:
        return jsonify({'error': 'Invalid agent token.'}), 401

    data = request.get_json(silent=True) or {}
    error = str(data.get('error', 'Unknown local-agent error'))[:4000]
    now = int(time.time())

    with _db() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'error',
                error = ?,
                updated_at = ?
            WHERE id = ? AND browser_token = ?
            """,
            (error, now, job_id, browser_token)
        )

    return jsonify({'success': True})


def _validate_proxy_url(proxy_url):
    if not proxy_url:
        return None

    if len(proxy_url) > 2048:
        raise ValueError("Adres proxy jest zbyt długi.")

    parsed = urlparse(proxy_url)
    allowed_schemes = {'http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h'}
    if parsed.scheme.lower() not in allowed_schemes:
        raise ValueError("Proxy musi używać http://, https://, socks4:// lub socks5://.")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("Nieprawidłowy adres proxy. Oczekiwany format: protokół://host:port")

    try:
        resolved = socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("Nie można rozwiązać hosta proxy.")

    for entry in resolved:
        ip = ipaddress.ip_address(entry[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError("Proxy musi wskazywać na publiczny adres IP.")

    return proxy_url


def _base_ydl_opts(user_proxy=None):
    opts = {
        'nocheckcertificate': True,
        'prefer_insecure': True,
    }
    cookies_exists = os.path.exists(COOKIES_FILE)
    cookies_size = os.path.getsize(COOKIES_FILE) if cookies_exists else 0
    print(f"[diag] COOKIES_FILE={COOKIES_FILE} exists={cookies_exists} size={cookies_size}bytes")
    if cookies_exists:
        opts['cookiefile'] = COOKIES_FILE
    if user_proxy:
        opts['proxy'] = user_proxy
    return opts


def _extract_with_fallback(url, extra_opts, download, user_proxy=None):
    last_error = None

    try:
        opts = dict(_base_ydl_opts(user_proxy))
        opts.update(extra_opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=download)
            if info:
                return info, 'default'
    except Exception as e:
        last_error = e
        print(f"[fallback] Domyślny klient zawiódł: {e}")
        time.sleep(1)

    for client in PLAYER_CLIENTS:
        opts = dict(_base_ydl_opts(user_proxy))
        opts.update(extra_opts)
        opts['extractor_args'] = {'youtube': {'player_client': [client]}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
                if info:
                    return info, client
        except Exception as e:
            last_error = e
            print(f"[fallback] Klient '{client}' zawiódł: {e}")
            time.sleep(1)
            continue
    raise last_error or Exception("Nie udało się pobrać danych żadnym z klientów YouTube.")


def download_media(url, media_type, timestamp=None, user_proxy=None):
    ext = ""
    downloaded_file = None
    final_filename = None
    info = None
    used_client = None
    user_proxy = _validate_proxy_url(user_proxy)

    if media_type == 'video':
        extra_opts = {
            'format': 'bv*+ba/b/best',
            'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'webm'}],
            'outtmpl': '%(id)s.%(ext)s',
        }
        ext = "webm"
        info, used_client = _extract_with_fallback(url, extra_opts, download=True, user_proxy=user_proxy)
    elif media_type == 'audio':
        extra_opts = {
            'format': 'ba*/b/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'vorbis'}],
            'outtmpl': '%(id)s.%(ext)s',
        }
        ext = "ogg"
        info, used_client = _extract_with_fallback(url, extra_opts, download=True, user_proxy=user_proxy)
    elif media_type == 'thumbnail':
        extra_opts = {
            'skip_download': True,
            'writethumbnail': True,
            'outtmpl': '%(id)s',
            'ignore_no_formats_error': True,
        }
        info, used_client = _extract_with_fallback(url, extra_opts, download=True, user_proxy=user_proxy)
    elif media_type == 'frame':
        extra_opts = {'format': 'b/bv*/best', 'skip_download': True}
        info, used_client = _extract_with_fallback(url, extra_opts, download=False, user_proxy=user_proxy)

    print(f"[info] Użyty klient YouTube: {used_client}")

    title_base = info.get('title', 'media')
    
    # Podstawowe czyszczenie tytułu
    safe_title = "".join([c for c in title_base if c.isalnum() or c == ' ']).rstrip().replace(" ", "_")

    # DODAJEMY PRZYROSTKI DO NAZWY PLIKU:
    if media_type == 'frame':
        s = float(timestamp)
        mm, ss = divmod(int(s), 60)
        # Używamy myślnika, aby zapobiec awariom na systemach Windows!
        safe_title += f"_(frame_{mm}-{ss:02d})"
    elif media_type == 'audio':
        safe_title += "_(audio)"
    elif media_type == 'thumbnail':
        safe_title += "_(thumbnail)"
    elif media_type == 'video':
        safe_title += "_(video)"


    if media_type == 'frame':
        stream_url = info.get('url')
        format_headers = {}

        if not stream_url and 'formats' in info:
            video_formats = [
                f for f in info['formats']
                if f.get('url') and f.get('vcodec') != 'none'
            ]

            if video_formats:
                selected_format = video_formats[-1]
                stream_url = selected_format['url']
                format_headers = selected_format.get('http_headers') or {}
        else:
            format_headers = info.get('http_headers') or {}

        if not stream_url:
            raise Exception(
                "Nie udało się uzyskać bezpośredniego strumienia wideo."
            )

        ext = 'jpg'
        final_filename = f"{safe_title}.jpg"

        headers = dict(info.get('http_headers') or {})
        headers.update(format_headers)

        user_agent = headers.pop(
            'User-Agent',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 Chrome/124 Safari/537.36'
        )

        referer = headers.pop(
            'Referer',
            'https://www.youtube.com/'
        )

        cmd = [
            'ffmpeg',
            '-ss', str(timestamp)
        ]

        if user_proxy:
            proxy_scheme = urlparse(user_proxy).scheme.lower()

            if proxy_scheme in ('http', 'https'):
                cmd += ['-http_proxy', user_proxy]
            elif proxy_scheme.startswith('socks'):
                raise ValueError(
                    "Dla typu 'frame' użyj proxy HTTP/HTTPS."
                )

        cmd += [
            '-user_agent', user_agent,
            '-referer', referer,
        ]

        if headers:
            header_string = ''.join(
                f"{key}: {value}\r\n"
                for key, value in headers.items()
            )
            cmd += ['-headers', header_string]

        cmd += [
            '-i', stream_url,
            '-vframes', '1',
            '-q:v', '2',
            final_filename,
            '-y'
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:
            print("[ffmpeg stderr]", result.stderr)
            raise Exception(
                "FFmpeg nie udało się pobrać klatki. "
                f"Szczegóły: {result.stderr[-2000:]}"
            )

        downloaded_file = final_filename

    elif media_type == 'thumbnail':
        downloaded_file = next((f for f in os.listdir('.') if f.startswith(info['id']) and f.endswith(('.jpg', '.webp', '.png'))), None)
        if not downloaded_file:
            downloaded_file = next((f for f in os.listdir('.') if f.endswith(('.jpg', '.webp', '.png'))), None)
        
        ext = downloaded_file.split('.')[-1] if downloaded_file else 'jpg'
        final_filename = f"{safe_title}.{ext}"
        
        if downloaded_file and downloaded_file != final_filename:
            os.replace(downloaded_file, final_filename)
        downloaded_file = final_filename

    else:
        downloaded_file = f"{info['id']}.{ext}"
        final_filename = f"{safe_title}.{ext}"
        if os.path.exists(downloaded_file) and downloaded_file != final_filename:
            os.replace(downloaded_file, final_filename)
            downloaded_file = final_filename


    upload_date = info.get('upload_date', '')
    if upload_date and upload_date >= '20250801':
        license_tag = '{{YouTube CC-BY-4.0}}'
    else:
        license_tag = '{{YouTube CC-BY}}'

    author = info.get('uploader', 'Unknown')
    tekst = upload_date
    wynik = f"{tekst[:4]}.{tekst[4:6]}.{tekst[6:]}"

    type_labels = {'video': 'film', 'audio': 'audio', 'thumbnail': 'thumbnail', 'frame': 'frame'}
    type_label = type_labels.get(media_type, media_type)

    if media_type == 'frame':
        seconds = float(timestamp)
        mm, ss = divmod(int(seconds), 60)
        time_label = f"{mm}:{ss:02d}"
        timestamped_link = f"[https://www.youtube.com/watch?v={info['id']}&t={int(seconds)}s {time_label}]"
        source_field = f"""Youtube Video: "{title_base}" {timestamped_link}"""
        top_line = f"""Frame at {time_label} from Youtube video "{title_base}" """
    elif media_type == 'audio':
        source_field = url
        top_line = f"""Audio  from Youtube video "{title_base}" """
    elif media_type == 'thumbnail':
        source_field = url
        top_line = f"""Thumbnail from Youtube video "{title_base}" """
    else:  
        source_field = url
        top_line = f"""Video from Youtube video "{title_base}" """

    full_description = f"{top_line}"

    description = (
        "== {{int:filedesc}} ==\n"
        "{{Information\n"
        f"|description={full_description}\n"
        f"|date={wynik}\n"
        f"|source={source_field}\n"
        f"|author={author}\n"
        "}}\n"
        "== {{int:license-header}} ==\n"
        f"{license_tag}\n"
        "{{LicenseReview}}"
    )

    comment = f"Uploaded a {type_label} by {author} from {url} with YouTube to Wikimedia Commons"

    return downloaded_file, final_filename, description, comment


def upload_to_commons(file_path, title, description, comment):
    headers = {
        'Authorization': f"Bearer {WIKI_ACCESS_TOKEN}",
        'User-Agent': 'YouTubeToCommonsLocal/1.0 (Contact: ToolforgeUser)'
    }

    res = requests.get(API_URL, params={'action': 'query', 'meta': 'tokens', 'format': 'json'}, headers=headers)
    print("[commons] tokens status:", res.status_code, res.text[:500])

    try:
        csrf_token = res.json()['query']['tokens']['csrftoken']
    except KeyError:
        raise Exception(f"Błąd uprawnień na Commons: {res.json()}")

    with open(file_path, 'rb') as f:
        files = {'file': (title, f, 'multipart/form-data')}
        data = {
            'action': 'upload',
            'filename': title,
            'text': description,
            'comment': comment,
            'token': csrf_token,
            'format': 'json',
            'ignorewarnings': 1
        }
        response = requests.post(API_URL, files=files, data=data, headers=headers)
        result = response.json()
        print("[commons] upload result:", result)

        if 'upload' in result and result['upload']['result'] == 'Success':
            return result['upload']['imageinfo']['descriptionurl']
        else:
            raise Exception(str(result))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug_mode, use_reloader=False, host='0.0.0.0', port=port)
