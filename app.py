import os
import time
import traceback
import subprocess
import socket
import ipaddress
from urllib.parse import urlparse
import requests
import yt_dlp
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'strong_random_session_secret_for_local_use')

API_URL = "https://commons.wikimedia.org/w/api.php"

# --- OWNER-ONLY OAUTH TOKEN ---
# Konsument jest zarejestrowany jako "owner-only", więc nie ma flow z przekierowaniem
# przez przeglądarkę (login/callback). Token jest stały i pobierany ze zmiennej
# środowiskowej WIKI_ACCESS_TOKEN.
#
# Lokalnie (PowerShell):
#   $env:WIKI_ACCESS_TOKEN = "twoj_token_tutaj"
#   python app.py
#
# Na Render: ustaw WIKI_ACCESS_TOKEN w panelu Environment (nie w kodzie, nie w repo).
WIKI_ACCESS_TOKEN = os.environ.get('WIKI_ACCESS_TOKEN')

if not WIKI_ACCESS_TOKEN:
    print("!!! UWAGA: Brak WIKI_ACCESS_TOKEN. Ustaw zmienną środowiskową,")
    print("!!! inaczej upload do Commons nie zadziała.")
else:
    print("DOTS:", WIKI_ACCESS_TOKEN.count('.'))
    print("LEN:", len(WIKI_ACCESS_TOKEN))

# Plik cookies eksportowany rozszerzeniem "Get cookies.txt LOCALLY" z youtube.com.
# Lokalnie: musi leżeć obok tego pliku app.py.
# Na Render: wgraj go jako "Secret File" (patrz README.md). Sekcja "Secret Files" na
# Render montuje pliki jako READ-ONLY, a yt-dlp domyślnie DOPISUJE zaktualizowane
# cookies z powrotem do cookiefile po każdym użyciu -> zapis się wywala
# ([Errno 30] Read-only file system). Dlatego przy starcie kopiujemy plik do /tmp
# (zapisywalne), i stamtąd już normalnie czytamy/nadpisujemy.
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

# Kolejność klientów YouTube do wypróbowania.
PLAYER_CLIENTS = ['mweb', 'android', 'web']


@app.route('/')
def index():
    # Skoro nie ma logowania przez przeglądarkę, "zalogowany" = token jest ustawiony w env.
    return render_template('index.html', logged_in=bool(WIKI_ACCESS_TOKEN))


@app.route('/check', methods=['POST'])
def check_license():
    """Weryfikuje, czy film posiada licencję Creative Commons."""
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
    user_proxy = request.form.get('proxy', '').strip() or None

    if not yt_url or media_type not in ['video', 'audio', 'thumbnail', 'frame']:
        return jsonify({'error': 'Missing required data.'}), 400

    try:
        downloaded_file, safe_title, description, comment = download_media(yt_url, media_type, timestamp, user_proxy=user_proxy)
        commons_url = upload_to_commons(downloaded_file, safe_title, description, comment)
        if os.path.exists(downloaded_file):
            os.remove(downloaded_file)
        return jsonify({'success': True, 'url': commons_url})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def _validate_proxy_url(proxy_url):
    """Waliduje opcjonalne proxy podane przez użytkownika.

    Dopuszczamy tylko HTTP/HTTPS/SOCKS4/SOCKS5. Proxy nie jest zapisywane
    i nie powinno być logowane, ponieważ może zawierać login/hasło.
    """
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

    # Ochrona przed użyciem pola proxy do łączenia się z localhostem / siecią
    # wewnętrzną serwera (SSRF). Dopuszczamy wyłącznie publiczne adresy IP.
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
    """
    Próba 0: domyślna, wielo-kliencka strategia yt-dlp (bez wymuszania player_client) -
    dokładnie to, czego używa /check, gdzie działa. yt-dlp sam dobiera i rotuje klientów,
    co bywa skuteczniejsze przeciwko bot-detection niż sztywne wymuszanie jednego na raz.

    Próby 1+: wymuszone pojedyncze klienty (mweb -> android -> web) jako plan B,
    na wypadek gdyby domyślna strategia zawiodła.
    """
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
        # ignore_no_formats_error: thumbnail nie potrzebuje odtwarzalnych formatów wideo/audio,
        # więc nie traktujemy braku formatów jako błąd krytyczny (tak jak w /check).
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
    safe_title = "".join([c for c in title_base if c.isalnum() or c == ' ']).rstrip().replace(" ", "_")

    if media_type == 'frame':
        stream_url = info.get('url')
        if not stream_url and 'formats' in info:
            video_formats = [f['url'] for f in info['formats'] if f.get('url')]
            if video_formats:
                stream_url = video_formats[-1]

        if not stream_url:
            raise Exception("Nie udało się uzyskać bezpośredniego strumienia wideo.")

        ext = 'jpg'
        final_filename = f"{safe_title}_{str(timestamp).replace('.', '_')}.jpg"

        cmd = ['ffmpeg', '-ss', str(timestamp)]
        # Dla klatki ffmpeg sam otwiera bezpośredni URL strumienia, więc musi
        # dostać to samo proxy. FFmpeg obsługuje http_proxy dla HTTP/HTTPS.
        if user_proxy:
            proxy_scheme = urlparse(user_proxy).scheme.lower()
            if proxy_scheme in ('http', 'https'):
                cmd += ['-http_proxy', user_proxy]
            elif proxy_scheme.startswith('socks'):
                raise ValueError("Dla typu 'frame' użyj proxy HTTP/HTTPS; SOCKS jest obsługiwany przez yt-dlp, ale nie przez ten etap ffmpeg.")

        cmd += ['-i', stream_url, '-vframes', '1', '-q:v', '2', final_filename, '-y']
        subprocess.run(cmd, check=True)
        downloaded_file = final_filename

    elif media_type == 'thumbnail':
        downloaded_file = next((f for f in os.listdir('.') if f.startswith(info['id']) and f.endswith(('.jpg', '.webp', '.png'))), None)
        if not downloaded_file:
            downloaded_file = next((f for f in os.listdir('.') if f.endswith(('.jpg', '.webp', '.png'))), None)
        ext = downloaded_file.split('.')[-1] if downloaded_file else 'jpg'
        final_filename = f"{safe_title}_thumb.{ext}"
        if downloaded_file and downloaded_file != final_filename:
            os.replace(downloaded_file, final_filename)
        downloaded_file = final_filename

    else:
        downloaded_file = f"{info['id']}.{ext}"
        final_filename = f"{safe_title}.{ext}"
        if os.path.exists(downloaded_file) and downloaded_file != final_filename:
            # FIX: os.rename -> os.replace, bo na Windows os.rename rzuca
            # [WinError 183] jeśli plik docelowy już istnieje (np. z poprzedniej,
            # przerwanej próby). os.replace nadpisuje bezpiecznie.
            os.replace(downloaded_file, final_filename)
            downloaded_file = final_filename

    # Dynamiczny dobór licencji
    upload_date = info.get('upload_date', '')
    if upload_date and upload_date >= '20250801':
        license_tag = '{{YouTube CC-BY-4.0}}'
    else:
        license_tag = '{{YouTube CC-BY}}'

    author = info.get('uploader', 'Unknown')
    video_description = info.get('description', '')
    tekst = upload_date
    wynik = f"{tekst[:4]}.{tekst[4:6]}.{tekst[6:]}"

    type_labels = {'video': 'film', 'audio': 'audio', 'thumbnail': 'thumbnail', 'frame': 'frame'}
    type_label = type_labels.get(media_type, media_type)

    if media_type == 'frame':
        seconds = float(timestamp)
        mm, ss = divmod(int(seconds), 60)
        time_label = f"{mm}:{ss:02d}"
        # FIX: był podwójny "?" (?v=...?t=...), co jest niepoprawnym URL-em.
        # Drugi parametr zapytania musi być po "&", nie po kolejnym "?".
        timestamped_link = f"[https://www.youtube.com/watch?v={info['id']}&t={int(seconds)}s {time_label}]"
        source_field = f"""Youtube Video: "{safe_title}" {timestamped_link}"""
        top_line = f"""Frame at {time_label} from Youtube video "{safe_title}" """
    elif media_type == 'audio':
        source_field = url
        top_line = f"""Audio  from Youtube video "{safe_title}" """
    elif media_type == 'thumbnail':
        source_field = url
        top_line = f"""Thumbnail from Youtube video "{safe_title}" """
    else:  # video
        source_field = url
        top_line = f"""Video from Youtube video "{safe_title}" """

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
    # WAŻNE: podmień "ToolforgeUser" na prawdziwy kontakt (email / stronę użytkownika na Commons).
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
    # FIX: debug=False domyślnie (bezpieczeństwo w produkcji) + port z env PORT (Render go wymaga).
    # Do lokalnego dev możesz ustawić FLASK_DEBUG=1 w env, żeby wrócić do trybu debug.
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug_mode, use_reloader=False, host='0.0.0.0', port=port
