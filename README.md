# YouTube to Wikimedia Commons

A web application hosted on Wikimedia Toolforge that allows users to easily extract and upload Creative Commons licensed media from YouTube directly to Wikimedia Commons. 

The tool was developed based on the source code and aims to simplify the process of enriching Wikimedia Commons with freely licensed video, audio, and images from YouTube.

## Features

* **OAuth 2.0 Integration:** Secure login using Wikimedia accounts.
* **Automatic License Verification:** Checks if the YouTube video is distributed under a Creative Commons (CC BY) license before allowing an upload.
* **Multiple Extraction Modes:**
  * **Video:** Converts and uploads the entire video as WebM.
  * **Audio:** Extracts the audio track and uploads it as Ogg.
  * **Thumbnail:** Downloads the highest resolution video thumbnail.
  * **Frame Capture:** Extracts a specific frame at a chosen timestamp as a JPEG.
* **Local Agent Technology:** A dedicated desktop agent that bypasses server-side YouTube IP blocks by downloading media on the user's local machine and uploading it directly to Commons.

## How to Use the Local Agent

To avoid YouTube proxy blocks on Toolforge, users can utilize the Local Agent.

### Option 1: Windows (Standalone executable)
1. Download the latest `YouTubeToCommonsAgent.exe` from the Releases page.
2. Run the application (it will register a custom URL protocol).
3. On the web interface, click **Pair local agent**. The browser will automatically send the pairing code to the app.

### Option 2: Mac / Linux / Advanced Users (Python Script)
1. Clone this repository and locate `agent.py`.
2. Install the required dependencies: `pip install requests yt-dlp`
3. Ensure `ffmpeg` is installed on your system (e.g., `brew install ffmpeg` or `sudo apt install ffmpeg`).
4. Run the script: `python3 agent.py`
5. On the web interface, click **Pair local agent** and manually paste the generated 6-digit code into your terminal.

## Server Deployment (Toolforge)

### Requirements
* Python 3.13+
* FFmpeg installed on the server

### Environment Variables

| Variable Name | Description | Required |
|---|---|---|
| FLASK_SECRET_KEY | Secret key for Flask sessions | Yes |
| CONSUMER_SECRET | Wikimedia OAuth 2.0 Client Secret | Yes |
| YOUTUBE_COOKIES_FILE | Path to cookies.txt to bypass age restrictions | No |
| PORT | Application listening port | No |

### Installation
1. Access your Toolforge account via SSH.
2. Clone the repository into your web directory.
3. Install Python requirements: `pip install -r requirements.txt`
4. Set the necessary environment variables.
5. Start or restart the web service using Kubernetes: `webservice --backend=kubernetes python3.13 restart`

---
*Disclaimer: This tool is intended for uploading CC-licensed content only. Users are responsible for ensuring they have the rights to upload the media to Wikimedia Commons.*
