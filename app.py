import os
import subprocess
import requests
import yt_dlp
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix

# Bezpieczne ładowanie ciasteczek ze zmiennych środowiskowych Render (jeśli istnieją)
COOKIE_FILE = None
if 'YOUTUBE_COOKIES' in os.environ and os.environ['YOUTUBE_COOKIES'].strip():
    COOKIE_FILE = '/tmp/youtube_cookies.txt'
    with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
        f.write(os.environ['YOUTUBE_COOKIES'])

app = Flask(__name__)
app.secret_key = 'strong_random_session_secret'
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

oauth = OAuth(app)
wikimedia = oauth.register(
    name='wikimedia',
    client_id='3767b334a535794d93f0911f29590b96',
    client_secret='504caa405e8edd8026b65166ffd017da16584336',
    access_token_url='https://meta.wikimedia.org/w/rest.php/oauth2/access_token',
    authorize_url='https://meta.wikimedia.org/w/rest.php/oauth2/authorize',
    api_base_url='https://commons.wikimedia.org/w/api.php'
)

API_URL = "https://commons.wikimedia.org/w/api.php"

@app.route('/')
def index():
    token = session.get('wiki_token')
    return render_template('index.html', logged_in=bool(token))

@app.route('/login')
def login():
    redirect_uri = url_for('auth', _external=True)
    return wikimedia.authorize_redirect(redirect_uri)

@app.route('/auth')
def auth():
    token = wikimedia.authorize_access_token()
    session['wiki_token'] = token
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('wiki_token', None)
    return redirect(url_for('index'))
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
            'ignore_no_formats_error': True,  # Ignoruje sprawdzanie formatów
            'check_formats': False,           # Wyłącza testowanie strumieni
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'ios', 'mweb', 'web']
                }
            }
        }
        if COOKIE_FILE:
            ydl_opts['cookiefile'] = COOKIE_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return jsonify({'is_cc': False, 'error': 'Failed to extract video info.'})

            license_info = info.get('license', '')
            
            # W niektórych wersjach yt-dlp licencja może być zapisana w opisie lub tagach
            description = info.get('description', '')
            
            is_creative_commons = False
            if license_info and ('Creative Commons' in license_info or 'Attribution' in license_info):
                is_creative_commons = True
            elif 'Creative Commons' in description or 'CC BY' in description:
                is_creative_commons = True

            if is_creative_commons:
                return jsonify({'is_cc': True, 'title': info.get('title'), 'id': info.get('id')})
            
            if not license_info:
                return jsonify({'is_cc': False, 'error': 'No Creative Commons license detected (standard YouTube license is not allowed on Commons).'})
            
            return jsonify({'is_cc': False, 'error': f'Invalid license: {license_info}'})
            
    except Exception as e:
        return jsonify({'is_cc': False, 'error': str(e)})

@app.route('/upload', methods=['POST'])
def handle_upload():
    token = session.get('wiki_token')
    if not token:
        return jsonify({'error': 'Not logged in.'}), 401

    yt_url = request.form.get('url')
    media_type = request.form.get('type')
    timestamp = request.form.get('timestamp')
    
    if not yt_url or media_type not in ['video', 'audio', 'thumbnail', 'frame']:
        return jsonify({'error': 'Missing required data.'}), 400

    try:
        downloaded_file, safe_title, description = download_media(yt_url, media_type, timestamp)
        commons_url = upload_to_commons(downloaded_file, safe_title, description, token)
        if os.path.exists(downloaded_file):
            os.remove(downloaded_file)
        return jsonify({'success': True, 'url': commons_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def download_media(url, media_type, timestamp=None):
    # Wykluczamy klienta 'web' i używamy wyłącznie klientów mobilnych/wbudowanych, 
    # które nie wyzwalają blokady botowej na adresach IP serwerowni.
    ydl_opts = {
        'extractor_args': {
            'youtube': {
                'player_client': ['mweb', 'ios', 'tv_embedded']
            }
        },
        'nocheckcertificate': True,
        'prefer_insecure': True
    }
    ext = ""
    
    if media_type == 'video':
        ydl_opts.update({
            'format': 'bv*+ba/b/best',
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'webm'
            }],
            'outtmpl': '%(id)s.%(ext)s'
        })
        ext = "webm"
    elif media_type == 'audio':
        # Pobieramy dowolny dostępny strumień audio i konwertujemy do OGG Vorbis
        ydl_opts.update({
            'format': 'bestaudio/ba/b/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'vorbis'
            }],
            'outtmpl': '%(id)s.%(ext)s'
        })
        ext = "ogg"
    elif media_type == 'thumbnail':
        ydl_opts.update({
            'skip_download': True, 
            'writethumbnail': True,
            'outtmpl': '%(id)s'
        })
    elif media_type == 'frame':
        ydl_opts.update({
            'format': 'b/bv*/best',
            'skip_download': True
        })

    if COOKIE_FILE:
        ydl_opts['cookiefile'] = COOKIE_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=(media_type in ['video', 'audio', 'thumbnail']))
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
            cmd = ['ffmpeg', '-ss', str(timestamp), '-i', stream_url, '-vframes', '1', '-q:v', '2', final_filename, '-y']
            subprocess.run(cmd, check=True)
            downloaded_file = final_filename
            
        elif media_type == 'thumbnail':
            downloaded_file = next((f for f in os.listdir('.') if f.startswith(info['id']) and f.endswith(('.jpg', '.webp', '.png'))), None)
            if not downloaded_file:
                downloaded_file = next((f for f in os.listdir('.') if f.endswith(('.jpg', '.webp', '.png'))), None)
            ext = downloaded_file.split('.')[-1] if downloaded_file else 'jpg'
            final_filename = f"{safe_title}_thumb.{ext}"
            if downloaded_file and downloaded_file != final_filename:
                os.rename(downloaded_file, final_filename)
            downloaded_file = final_filename
            
        else:
            # Dla wideo i audio yt-dlp po konwersji przez FFmpeg zapisuje plik z nowym rozszerzeniem
            downloaded_file = f"{info['id']}.{ext}"
            final_filename = f"{safe_title}.{ext}"
            if os.path.exists(downloaded_file) and downloaded_file != final_filename:
                os.rename(downloaded_file, final_filename)
                downloaded_file = final_filename

        # Dynamiczny wybór licencji (od 1 sierpnia 2025 r. CC BY-4.0)
        upload_date = info.get('upload_date', '')
        if upload_date and upload_date >= '20250801':
            license_tag = '{{YouTube CC-BY-4.0}}'
        else:
            license_tag = '{{YouTube CC-BY}}'

        description = (
            "== {{int:filedesc}} ==\n"
            "{{Information\n"
            f"|description={info.get('description', 'Downloaded from YouTube')}\n"
            f"|date={upload_date}\n"
            f"|source={url}\n"
            f"|author={info.get('uploader', 'Unknown')}\n"
            f"|permission={license_tag}\n"
            "}}\n"
            "== {{int:license-header}} ==\n"
            f"{license_tag}\n"
            "{{LicenseReview}}"
        )
        return downloaded_file, final_filename, description

def upload_to_commons(file_path, title, description, token):
    headers = {'Authorization': f"Bearer {token['access_token']}"}
    res = requests.get(API_URL, params={'action': 'query', 'meta': 'tokens', 'format': 'json'}, headers=headers)
    csrf_token = res.json()['query']['tokens']['csrftoken']
    
    with open(file_path, 'rb') as f:
        files = {'file': (title, f, 'multipart/form-data')}
        data = {
            'action': 'upload',
            'filename': title,
            'text': description,
            'token': csrf_token,
            'format': 'json',
            'ignorewarnings': 1
        }
        response = requests.post(API_URL, files=files, data=data, headers=headers)
        result = response.json()
        
        if 'upload' in result and result['upload']['result'] == 'Success':
            return result['upload']['imageinfo']['descriptionurl']
        else:
            raise Exception(str(result))
