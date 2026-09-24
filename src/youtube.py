from __future__ import annotations
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request


def service(cid, secret, refresh):
    c = Credentials(
        None, refresh_token=refresh,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=cid, client_secret=secret,
        scopes=['https://www.googleapis.com/auth/youtube']
    )
    c.refresh(Request())
    return build('youtube', 'v3', credentials=c, cache_discovery=False)


class YouTube:
    def __init__(self, cid, secret, refresh, retry):
        self.s = service(cid, secret, refresh)
        self.retry = retry

    def _uploads_playlist(self):
        r = self.retry(
            lambda: self.s.channels().list(part='contentDetails', mine=True).execute(),
            'youtube'
        )
        items = r.get('items', [])
        if not items:
            raise RuntimeError('No YouTube channel is available for the supplied credentials')
        return items[0]['contentDetails']['relatedPlaylists']['uploads']

    def find_existing_by_marker(self, book_id):
        """Recover an upload if the runner crashed after YouTube accepted it."""
        marker = f'AI-AUDIOBOOK-ID:{book_id}'
        playlist = self._uploads_playlist()
        token = None
        while True:
            page = self.retry(
                lambda: self.s.playlistItems().list(
                    part='contentDetails', playlistId=playlist,
                    maxResults=50, pageToken=token
                ).execute(), 'youtube'
            )
            ids = [x['contentDetails']['videoId'] for x in page.get('items', [])]
            if ids:
                details = self.retry(
                    lambda: self.s.videos().list(part='id,snippet', id=','.join(ids)).execute(),
                    'youtube'
                )
                for item in details.get('items', []):
                    if marker in (item.get('snippet', {}).get('description') or ''):
                        return item['id']
            token = page.get('nextPageToken')
            if not token:
                break
        return None

    def set_thumbnail(self, video_id, thumbnail):
        self.retry(
            lambda: self.s.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail), mimetype='image/png')
            ).execute(), 'youtube'
        )

    def upload(self, path, title, desc, tags, privacy, thumbnail=None):
        body = {
            'snippet': {'title': title, 'description': desc, 'tags': tags, 'categoryId': '27'},
            'status': {'privacyStatus': privacy, 'selfDeclaredMadeForKids': False}
        }
        r = self.retry(
            lambda: self.s.videos().insert(
                part='snippet,status', body=body,
                media_body=MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True)
            ).execute(), 'youtube'
        )
        vid = r['id']
        if thumbnail:
            self.set_thumbnail(vid, thumbnail)
        check = self.retry(
            lambda: self.s.videos().list(part='id,status,snippet', id=vid).execute(), 'youtube'
        )
        if not check.get('items') or check['items'][0]['id'] != vid:
            raise RuntimeError('YouTube upload verification failed')
        return vid
