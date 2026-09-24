# Autonomous YouTube Audiobook System — Production Repair

This revision fixes the critical recovery and publishing defects found in the previous ZIP.

## Required Drive layout

The service account must have access to the Drive space used by the pipeline. The application creates/uses:

```text
AUDIOBOOK_AUTOMATION/
├── INPUT_ASSETS/
│   ├── covers/
│   │   └── 001.png
│   └── music/
├── VIDEO_LIBRARY/
│   ├── Person A/
│   ├── Person B/
│   └── ...
├── WORK/
├── SUCCESS/
└── FAILED/
```

Video clips should already contain their intended PIP overlays. PIP processing was removed from the production configuration.

## GitHub input

`input/topics.txt`:

```text
001|Book topic here
002|Another book topic
```

Large covers, music and video are kept in Drive instead of GitHub.

## Secrets

- `GEMINI_API_KEY`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`
- `GOOGLE_DRIVE_CREDENTIALS`

### Important YouTube authorization change

The repair uses the broader `youtube` OAuth scope so it can verify whether a video was already published after a runner crash. An old refresh token created only with `youtube.upload` may need to be re-authorized.

## Recovery model

Every expensive completed artifact is checkpointed to `WORK/{book_id}/` immediately after creation:

- outline
- introduction
- every chapter
- final script
- every TTS WAV chunk
- final audio
- video source plan
- background video
- final video
- thumbnail
- metadata
- state

A fresh GitHub runner can therefore reconstruct its local working directory from Drive instead of regenerating completed work.

`FAILED` is **not terminal**. The scheduler retries failed books on later runs. Quota failures use `WAITING_FOR_QUOTA`.

## TTS

TTS order is:

1. Microsoft Edge Neural TTS
2. Kokoro fallback

Gemini TTS is not used.

Voice speed and pitch remain controlled through `voice.json`.

## Background music

Music is actually mixed into the final video at `music_volume`, looped to the narration duration and placed underneath narration.

## Scheduler

The application no longer requires GitHub Actions to start at exactly `04:00`. GitHub scheduled workflows can be delayed; any scheduled start is accepted.

## Validation performed on this repair

- Python syntax compilation for every source module
- ZIP rebuilt from the repaired repository
- Critical configuration/code paths inspected statically
