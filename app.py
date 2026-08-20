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
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from requests_oauthlib import OAuth2Session
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
# ProxyFix naprawia linki powrotne (callback) działające za serwerami Toolforge
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'strong_random_session_secret_for_local_use')

API_URL = "https://commons.wikimedia.org/w/api.php"

# --- OAUTH 2.0 KONFIGURACJA ---
CLIENT_ID = "d9963eb17353ebc8774819c2983e1fa2"
CLIENT_SECRET = os.environ.get('CONSUMER_SECRET')
AUTHORIZATION_BASE_URL = 'https://meta.wikimedia.org/w/rest.php/oauth2/authorize'
TOKEN_URL = 'https://meta.wikimedia.org/w/rest.php/oauth2/access_token'

if not CLIENT_SECRET:
    print("!!! UWAGA: Brak CONSUMER_SECRET w zmiennych środowiskowych serwera!")

_SOURCE_COOKIES_FILE = os.environ.get('YOUTUBE_COOKIES_FILE', 'youtube_cookies.txt')
COOKIES_FILE = _SOURCE_COOKIES_FILE

if os.path.exists(_SOURCE_COOKIES_FILE):
    try:
        writable_path = os.path.join('/tmp', 'youtube_cookies.txt')
        if os.path.abspath(_SOURCE_COOKIES_FILE) != os.path.abspath(writable_path):
            import shutil
            shutil.copyfile(_SOURCE_COOKIES_FILE, writable_path)
            COOKIES_FILE = writable_path
            print(f"[cookies] Skopiowano {_SOURCE_COOKIES_FILE} -> {writable_path}")
    except Exception as e:
        print(f"[cookies] UWAGA: nie udało się skopiować cookies do /tmp: {e}")

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
                oauth_token TEXT,
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
        # Dodanie nowych kolumn jeśli nie istnieją
       # Dodanie nowych kolumn jeśli nie istnieją
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN custom_license TEXT")
        except sqlite3.OperationalError:
            pass
            
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN categories TEXT")
        except sqlite3.OperationalError:
            pass

_init_agent_db()

# --- SYSTEM LOGOWANIA ---

@app.route('/login')
def login():
    client = OAuth2Session(CLIENT_ID)
    authorization_url, state = client.authorization_url(AUTHORIZATION_BASE_URL)
    session['oauth_state'] = state
    return redirect(authorization_url)

@app.route('/oauth-callback')
def callback():
    try:
        client = OAuth2Session(CLIENT_ID, state=session.get('oauth_state'))
        token = client.fetch_token(TOKEN_URL, client_secret=CLIENT_SECRET, authorization_response=request.url)
        session['oauth_token'] = token['access_token']
    except Exception as e:
        print(f"OAuth Callback Error: {e}")
        return "Błąd logowania. Spróbuj ponownie."
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('oauth_token', None)
    return redirect(url_for('index'))

@app.route('/')
def index():
    return render_template('index.html', logged_in='oauth_token' in session)

# --- OBSŁUGA APLIKACJI ---

@app.route('/check', methods=['POST'])
def check_license():
    url = request.json.get('url')
    manual_license = request.json.get('manual_license')
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
                
            # Obejście licencji (Bypass)
            if manual_license:
                return jsonify({'is_cc': True, 'title': info.get('title'), 'id': info.get('id')})

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
                return jsonify({'is_cc': False, 'error': 'Brak licencji CC.'})

            return jsonify({'is_cc': False, 'error': f'Invalid license: {license_info}'})
    except Exception as e:
        return jsonify({'is_cc': False, 'error': str(e)})

@app.route('/upload', methods=['POST'])
def handle_upload():
    access_token = session.get('oauth_token')
    if not access_token:
        return jsonify({'error': 'Nie jesteś zalogowany.'}), 401

    yt_url = request.form.get('url')
    media_type = request.form.get('type')
    timestamp = request.form.get('timestamp')
    download_mode = (request.form.get('download_mode') or 'proxy').strip().lower()
    user_proxy = request.form.get('proxy', '').strip() or None
    custom_license = request.form.get('custom_license', '').strip()
    categories = request.form.get('categories', '').strip()

    if download_mode != 'proxy':
        return jsonify({'error': 'Invalid download mode.'}), 400

    try:
        downloaded_file, safe_title, description, comment = download_media(
            yt_url, media_type, timestamp, user_proxy=user_proxy, custom_license=custom_license, categories=categories
        )
        commons_url = upload_to_commons(downloaded_file, safe_title, description, comment, access_token)
        if os.path.exists(downloaded_file):
            os.remove(downloaded_file)
        return jsonify({'success': True, 'url': commons_url})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/local/pair/start', methods=['POST'])
def local_pair_start():
    access_token = session.get('oauth_token')
    if not access_token:
        return jsonify({'error': 'Nie jesteś zalogowany.'}), 401

    now = int(time.time())
    pairing_code = f"{secrets.randbelow(1000000):06d}"
    browser_token = secrets.token_urlsafe(32)
    expires_at = now + 86400

    with _db() as conn:
        conn.execute(
            """
            INSERT INTO pairings
                (pairing_code, browser_token, oauth_token, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (pairing_code, browser_token, access_token, now, expires_at)
        )

    return jsonify({'pairing_code': pairing_code, 'browser_token': browser_token, 'expires_in': 86400})

@app.route('/api/local/pair/status', methods=['GET'])
def local_pair_status():
    browser_token = request.headers.get('X-Browser-Token', '').strip()
    now = int(time.time())
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM pairings WHERE browser_token = ? AND agent_token IS NOT NULL AND expires_at > ?",
            (browser_token, now)
        ).fetchone()
    return jsonify({'paired': bool(row)})

@app.route('/api/agent/pair', methods=['POST'])
def agent_pair():
    data = request.get_json(silent=True) or {}
    pairing_code = str(data.get('pairing_code', '')).strip()

    if not pairing_code:
        return jsonify({'error': 'Missing pairing code.'}), 400

    now = int(time.time())
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM pairings WHERE pairing_code = ? AND agent_token IS NULL AND expires_at > ?",
            (pairing_code, now)
        ).fetchone()

        if not row:
            return jsonify({'error': 'Invalid or expired pairing code.'}), 404

        agent_token = secrets.token_urlsafe(48)
        conn.execute("UPDATE pairings SET agent_token = ? WHERE id = ?", (agent_token, row['id']))

    return jsonify({'agent_token': agent_token})

@app.route('/api/local/jobs', methods=['POST'])
def local_create_job():
    access_token = session.get('oauth_token')
    if not access_token:
        return jsonify({'error': 'Nie jesteś zalogowany.'}), 401

    browser_token = request.headers.get('X-Browser-Token', '').strip()
    data = request.get_json(silent=True) or {}
    url = str(data.get('url', '')).strip()
    media_type = str(data.get('type', '')).strip()
    timestamp = data.get('timestamp')
    custom_license = str(data.get('custom_license', '')).strip()
    categories = str(data.get('categories', '')).strip()

    now = int(time.time())
    job_id = uuid.uuid4().hex

    with _db() as conn:
        conn.execute(
            """
            INSERT INTO jobs
                (id, browser_token, url, media_type, timestamp, custom_license, categories, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?)
            """,
            (job_id, browser_token, url, media_type, None if timestamp is None else str(timestamp), custom_license, categories, now, now)
        )
    return jsonify({'job_id': job_id, 'status': 'waiting'})

@app.route('/api/local/jobs/<job_id>', methods=['GET'])
def local_job_status(job_id):
    browser_token = request.headers.get('X-Browser-Token', '').strip()
    with _db() as conn:
        row = conn.execute("SELECT id, status, error, commons_url FROM jobs WHERE id = ? AND browser_token = ?", (job_id, browser_token)).fetchone()
    if not row:
        return jsonify({'error': 'Job not found.'}), 404
    return jsonify(dict(row))

def _bearer_token():
    value = request.headers.get('Authorization', '')
    if value.lower().startswith('bearer '):
        return value[7:].strip()
    return None

@app.route('/api/agent/jobs/next', methods=['GET'])
def agent_next_job():
    agent_token = _bearer_token()
    now = int(time.time())

    with _db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        browser_row = conn.execute("SELECT browser_token FROM pairings WHERE agent_token = ? AND expires_at > ?", (agent_token, now)).fetchone()
        
        if not browser_row:
            conn.commit()
            return jsonify({'error': 'Invalid agent token.'}), 401
            
        browser_token = browser_row['browser_token']
        row = conn.execute(
            "SELECT id, url, media_type AS type, timestamp, custom_license, categories FROM jobs WHERE browser_token = ? AND status = 'waiting' ORDER BY created_at ASC LIMIT 1",
            (browser_token,)
        ).fetchone()

        if not row:
            conn.commit()
            return ('', 204)

        conn.execute("UPDATE jobs SET status = 'processing', updated_at = ? WHERE id = ?", (now, row['id']))
        conn.commit()

    return jsonify(dict(row))

@app.route('/api/agent/jobs/<job_id>/result', methods=['POST'])
def agent_job_result(job_id):
    agent_token = _bearer_token()
    now = int(time.time())

    with _db() as conn:
        pairing = conn.execute("SELECT browser_token, oauth_token FROM pairings WHERE agent_token = ? AND expires_at > ?", (agent_token, now)).fetchone()
        if not pairing:
            return jsonify({'error': 'Invalid agent token.'}), 401

        browser_token = pairing['browser_token']
        access_token = pairing['oauth_token']

        job = conn.execute("SELECT id FROM jobs WHERE id = ? AND browser_token = ? AND status = 'processing'", (job_id, browser_token)).fetchone()
        if not job:
            return jsonify({'error': 'Job not found.'}), 404

    uploaded = request.files.get('file')
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '')
    comment = request.form.get('comment', '')

    suffix = os.path.splitext(title)[1]
    fd, temp_path = tempfile.mkstemp(prefix='yttocommons_agent_', suffix=suffix)
    os.close(fd)

    try:
        uploaded.save(temp_path)
        commons_url = upload_to_commons(temp_path, title, description, comment, access_token)
        
        with _db() as conn:
            conn.execute("UPDATE jobs SET status = 'done', commons_url = ?, updated_at = ? WHERE id = ?", (commons_url, now, job_id))
        return jsonify({'success': True, 'url': commons_url})
    except Exception as e:
        with _db() as conn:
            conn.execute("UPDATE jobs SET status = 'error', error = ?, updated_at = ? WHERE id = ?", (str(e), now, job_id))
        # Zmiana z 500 na 200 by zapobiec nadpisywaniu błędu przez Agenta
        return jsonify({'error': str(e)}), 200
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

@app.route('/api/agent/jobs/<job_id>/error', methods=['POST'])
def agent_job_error(job_id):
    agent_token = _bearer_token()
    now = int(time.time())
    with _db() as conn:
        browser_row = conn.execute("SELECT browser_token FROM pairings WHERE agent_token = ? AND expires_at > ?", (agent_token, now)).fetchone()
        if not browser_row:
            return jsonify({'error': 'Invalid agent token.'}), 401

        data = request.get_json(silent=True) or {}
        error = str(data.get('error', 'Unknown local-agent error'))[:4000]
        conn.execute("UPDATE jobs SET status = 'error', error = ?, updated_at = ? WHERE id = ? AND browser_token = ?", (error, now, job_id, browser_row['browser_token']))
    return jsonify({'success': True})

def _validate_proxy_url(proxy_url):
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    allowed_schemes = {'http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h'}
    if parsed.scheme.lower() not in allowed_schemes:
        raise ValueError("Proxy musi używać http://, https://, socks4:// lub socks5://.")
    return proxy_url

def _base_ydl_opts(user_proxy=None):
    opts = {'nocheckcertificate': True, 'prefer_insecure': True}
    if os.path.exists(COOKIES_FILE):
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
            if info: return info, 'default'
    except Exception as e:
        last_error = e
        time.sleep(1)

    for client in PLAYER_CLIENTS:
        opts = dict(_base_ydl_opts(user_proxy))
        opts.update(extra_opts)
        opts['extractor_args'] = {'youtube': {'player_client': [client]}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
                if info: return info, client
        except Exception as e:
            last_error = e
            time.sleep(1)
            continue
    raise last_error or Exception("Nie udało się pobrać danych żadnym z klientów YouTube.")

def download_media(url, media_type, timestamp=None, user_proxy=None, custom_license="", categories=""):
    ext = ""
    downloaded_file = None
    final_filename = None
    info = None
    user_proxy = _validate_proxy_url(user_proxy)

    if media_type == 'video':
        extra_opts = {'format': 'bv*+ba/b/best', 'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'webm'}], 'outtmpl': '%(id)s.%(ext)s'}
        ext, info, _ = "webm", *_extract_with_fallback(url, extra_opts, download=True, user_proxy=user_proxy)
    elif media_type == 'audio':
        extra_opts = {'format': 'ba*/b/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'vorbis'}], 'outtmpl': '%(id)s.%(ext)s'}
        ext, info, _ = "ogg", *_extract_with_fallback(url, extra_opts, download=True, user_proxy=user_proxy)
    elif media_type == 'thumbnail':
        extra_opts = {'skip_download': True, 'writethumbnail': True, 'outtmpl': '%(id)s', 'ignore_no_formats_error': True}
        info, _ = _extract_with_fallback(url, extra_opts, download=True, user_proxy=user_proxy)
    elif media_type == 'frame':
        extra_opts = {'format': 'b/bv*/best', 'skip_download': True}
        info, _ = _extract_with_fallback(url, extra_opts, download=False, user_proxy=user_proxy)

    title_base = info.get('title', 'media')
    safe_title = "".join([c for c in title_base if c.isalnum() or c == ' ']).rstrip().replace(" ", "_")

    if media_type == 'frame':
        s = float(timestamp)
        mm, ss = divmod(int(s), 60)
        safe_title += f"_(frame_{mm}-{ss:02d})"
        stream_url = info.get('url')
        if not stream_url and 'formats' in info:
            video_formats = [f for f in info['formats'] if f.get('url') and f.get('vcodec') != 'none']
            if video_formats: stream_url = video_formats[-1]['url']
        
        ext, final_filename = 'jpg', f"{safe_title}.jpg"
        cmd = ['ffmpeg', '-ss', str(timestamp), '-i', stream_url, '-vframes', '1', '-q:v', '2', final_filename, '-y']
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        downloaded_file = final_filename
    elif media_type == 'thumbnail':
        downloaded_file = next((f for f in os.listdir('.') if f.startswith(info['id']) and f.endswith(('.jpg', '.webp', '.png'))), None)
        ext = downloaded_file.split('.')[-1] if downloaded_file else 'jpg'
        final_filename = f"{safe_title}.{ext}"
        if downloaded_file and downloaded_file != final_filename:
            os.replace(downloaded_file, final_filename)
        downloaded_file = final_filename
    else:
        if media_type == 'video': safe_title += "_(video)"
        elif media_type == 'audio': safe_title += "_(audio)"
        
        downloaded_file = f"{info['id']}.{ext}"
        final_filename = f"{safe_title}.{ext}"
        if os.path.exists(downloaded_file) and downloaded_file != final_filename:
            os.replace(downloaded_file, final_filename)
            downloaded_file = final_filename

    upload_date = info.get('upload_date', '')
    
    # Podmiana licencji
    if custom_license:
        license_tag = custom_license if custom_license.startswith("{{") else f"{{{{{custom_license}}}}}"
    else:
        license_tag = '{{YouTube CC-BY 4.0}}' if upload_date and upload_date >= '20250801' else '{{YouTube CC-BY}}'

    # Przygotowanie kategorii
    cat_text = ""
    if categories:
        cat_list = [c.strip() for c in categories.split(",") if c.strip()]
        for c in cat_list:
            if not c.lower().startswith("category:"):
                c = f"Category:{c}"
            cat_text += f"[[{c}]]\n"
    if not cat_text:
        cat_text = "[[Category:Uploaded with Youtube to Wikimedia Commons]]"

    author = info.get('uploader', 'Unknown')
    wynik = f".{upload_date[4:6]}.{upload_date[6:]}.{upload_date[:4]}" if upload_date else ""

    if media_type == 'frame':
        seconds = float(timestamp)
        mm, ss = divmod(int(seconds), 60)
        source_field = f"""Youtube Video: "{title_base}" [https://www.youtube.com/watch?v={info['id']}&t={int(seconds)}s {mm}:{ss:02d}]"""
    else:  
        source_field = url

    description = (
        "== {{int:filedesc}} ==\n"
        "{{Information\n"
        f"|description=Media from Youtube video \"{title_base}\"\n"
        f"|date={wynik}\n"
        f"|source={source_field}\n"
        f"|author={author}\n"
        "}}\n"
        "== {{int:license-header}} ==\n"
        f"{license_tag}\n"
        "{{LicenseReview}}\n\n"
        f"{cat_text}"
    )

    return downloaded_file, final_filename, description, f"Uploaded a {media_type} by {author} from {url} with YouTube to Wikimedia Commons"

def upload_to_commons(file_path, title, description, comment, access_token):
    headers = {
        'Authorization': f"Bearer {access_token}",
        'User-Agent': 'YouTubeToCommons/1.2 (Contact: Twój_Kontakt)'
    }
    
    # 1. Pobieranie biletu (tokena) z obsługą błędów API
    res = requests.get(API_URL, params={'action': 'query', 'meta': 'tokens', 'format': 'json'}, headers=headers)
    data = res.json()
    
    if 'error' in data:
        raise Exception(f"Wikimedia API Error: {data['error'].get('info', str(data['error']))}")
    if 'query' not in data:
        raise Exception(f"Unexpected response from Wikimedia: {str(data)}")
        
    csrf_token = data['query']['tokens']['csrftoken']

    # 2. Wysyłanie pliku z dokładnym sprawdzaniem odpowiedzi
    with open(file_path, 'rb') as f:
        files = {'file': (title, f, 'multipart/form-data')}
        payload = {'action': 'upload', 'filename': title, 'text': description, 'comment': comment, 'token': csrf_token, 'format': 'json', 'ignorewarnings': 1}
        
        response = requests.post(API_URL, files=files, data=payload, headers=headers)
        result = response.json()
        
        if 'upload' in result and result['upload']['result'] == 'Success':
            return result['upload']['imageinfo']['descriptionurl']
        
        if 'error' in result:
            raise Exception(f"Wikimedia Upload Failed: {result['error'].get('info', str(result['error']))}")
            
        raise Exception(f"Unknown API error: {str(result)}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, use_reloader=False, host='0.0.0.0', port=port)
