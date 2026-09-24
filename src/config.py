from __future__ import annotations
import json, os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def load_json(name):
    with open(ROOT/name, encoding='utf-8') as f: return json.load(f)

def env(name, required=True):
    v=os.getenv(name)
    if required and not v: raise RuntimeError(f'Missing required secret: {name}')
    return v

CONFIG=load_json('config.json')
VOICE=load_json('voice.json')
TIMEZONE=CONFIG['timezone']
DRIVE_ROOT='AUDIOBOOK_AUTOMATION'

def secret_config():
    return {
      'gemini_api_key': env('GEMINI_API_KEY'),
      'youtube_client_id': env('YOUTUBE_CLIENT_ID'),
      'youtube_client_secret': env('YOUTUBE_CLIENT_SECRET'),
      'youtube_refresh_token': env('YOUTUBE_REFRESH_TOKEN'),
      'google_drive_credentials': env('GOOGLE_DRIVE_CREDENTIALS')
    }
