import os
import subprocess
import requests
import yt_dlp
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.secret_key = 'silne_losowe_haslo_sesji'
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

oauth = OAuth(app)
wikimedia = oauth.register(
    name='wikimedia',
    client_id='TUTAJ_WPISZ_CLIENT_ID',
    client_secret='TUTAJ_WPISZ_CLIENT_SECRET',
    access_token_url='https://meta.wikimedia.org/w/rest.php/oauth2/access_token',
    authorize_url='https://meta.wikimedia.org/w/rest.php/oauth2/authorize',
    api_base_url='https://commons.wikimedia.org/w/api.php',
    client_kwargs={'scope': 'edit upload'}
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
        return jsonify({'is_cc': False, 'error': 'Brak linku URL.'})
    
    try:
        # skip_download sprawia, że yt-dlp tylko czyta informacje, co jest błyskawiczne
        ydl_opts = {'quiet': True, 'skip_download': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            license_info = info.get('license', '')
            
            if not license_info:
                return jsonify({'is_cc': False, 'error': 'Brak licencji w opisie. Prawdopodobnie jest to standardowa licencja YouTube (zabroniona na Commons).'})
            
            if 'Creative Commons' in license_info or 'Attribution' in license_info:
                return jsonify({'is_cc': True, 'title': info.get('title'), 'id': info.get('id')})
            
            return jsonify({'is_cc': False, 'error': f'Nieprawidłowa licencja: {license_info}'})
    except Exception as e:
        return jsonify({'is_cc': False, 'error': str(e)})

@app.route('/upload', methods=['POST'])
def handle_upload():
    token = session.get('wiki_token')
    if not token:
        return jsonify({'error': 'Nie zalogowano.'}), 401

    yt_url = request.form.get('url')
    media_type = request.form.get('type')
    timestamp = request.form.get('timestamp') # Wymagane tylko dla klatki
    
    if not yt_url or media_type not in ['video', 'audio', 'thumbnail', 'frame']:
        return jsonify({'error': 'Brak wymaganych danych.'}), 400

    try:
        downloaded_file, safe_title, description = download_media(yt_url, media_type, timestamp)
        commons_url = upload_to_commons(downloaded_file, safe_title, description, token)
        os.remove(downloaded_file)
        return jsonify({'success': True, 'url': commons_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def download_media(url, media_type, timestamp=None):
    ydl_opts = {}
    ext = ""
    
    if media_type == 'video':
        ydl_opts = {'format': 'bestvideo+bestaudio/best', 'merge_output_format': 'webm'}
        ext = "webm"
    elif media_type == 'audio':
        ydl_opts = {'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'vorbis'}]}
        ext = "ogg"
    elif media_type == 'thumbnail':
        ydl_opts = {'skip_download': True, 'writethumbnail': True}
    elif media_type == 'frame':
        ydl_opts = {'format': 'bestvideo', 'skip_download': True} # Pobieramy tylko link do strumienia

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=(media_type in ['video', 'audio', 'thumbnail']))
        title_base = info.get('title', 'media')
        
        # Filtrujemy tytuł, by był bezpieczny dla nazw plików w systemie
        safe_title = "".join([c for c in title_base if c.isalpha() or c.isdigit() or c==' ']).rstrip().replace(" ", "_")
        
        if media_type == 'frame':
            # Używamy FFmpeg do pobrania 1 klatki bezpośrednio ze strumienia YouTube
            stream_url = info['url']
            ext = 'jpg'
            downloaded_file = f"{safe_title}_{str(timestamp).replace('.', '_')}.jpg"
            cmd = ['ffmpeg', '-ss', str(timestamp), '-i', stream_url, '-vframes', '1', '-q:v', '2', downloaded_file, '-y']
            subprocess.run(cmd, check=True)
            final_filename = downloaded_file
        elif media_type == 'thumbnail':
            downloaded_file = next((f for f in os.listdir('.') if f.startswith(info['id']) or info['id'] in f), None)
            if not downloaded_file:
                 downloaded_file = next((f for f in os.listdir('.') if f.endswith(('.jpg', '.webp'))), None)
            ext = downloaded_file.split('.')[-1]
            final_filename = f"{safe_title}_thumb.{ext}"
            os.rename(downloaded_file, final_filename)
            downloaded_file = final_filename
        else:
            downloaded_file = f"{info['id']}.{ext}"
            final_filename = f"{safe_title}.{ext}"
            if os.path.exists(downloaded_file):
                os.rename(downloaded_file, final_filename)
                downloaded_file = final_filename

        # Szablon {{LicenseReview}} flaguje plik do manualnego sprawdzenia przez adminów
        description = (
            "== {{int:filedesc}} ==\n"
            "{{Information\n"
            f"|description={info.get('description', 'Pobrane z YouTube')}\n"
            f"|date={info.get('upload_date', '')}\n"
            f"|source={url}\n"
            f"|author={info.get('uploader', 'Nieznany')}\n"
            "|permission={{YouTube CC-BY}}\n"
            "}}\n"
            "== {{int:license-header}} ==\n"
            "{{YouTube CC-BY}}\n"
            "{{LicenseReview}}"
        )
        return downloaded_file, final_filename, description

def upload_to_commons(file_path, title, description, token):
    headers = {'Authorization': f"Bearer {token['access_token']}"}
    res = requests.get(API_URL, params={'action': 'query', 'meta': 'tokens', 'format': 'json'}, headers=headers)
    csrf_token = res.json()['query']['tokens']['csrftoken']
    
    with open(file_path, 'rb') as f:
        files = {'file': (title, f, 'multipart/form-data')}
        data = {'action': 'upload', 'filename': title, 'text': description, 'token': csrf_token, 'format': 'json', 'ignorewarnings': 1}
        response = requests.post(API_URL, files=files, data=data, headers=headers)
        result = response.json()
        
        if 'upload' in result and result['upload']['result'] == 'Success':
            return result['upload']['imageinfo']['descriptionurl']
        else:
            raise Exception(str(result))
