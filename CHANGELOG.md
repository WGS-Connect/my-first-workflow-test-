# Critical Repair Changelog

## Production recovery repair

Fixed the following deployment-blocking defects from the previous build:

1. `FAILED` books are retryable; only `SUCCESS` is terminal.
2. Completed outline, intro, chapters, script, TTS chunks, audio, video plan, background video, final video, thumbnail and metadata are checkpointed to Drive.
3. Fresh runners restore completed artifacts instead of regenerating them.
4. Drive uploads are streaming/resumable and no longer load large files into RAM.
5. YouTube publication has a durable book marker and post-crash discovery path; an existing publication is recovered instead of duplicated.
6. Quota failures use `WAITING_FOR_QUOTA`.
7. Retry handling recognizes HTTP/rate-limit/server/quota classes rather than only message strings.
8. Background music is actually looped, volume-controlled and mixed under narration.
9. Video source plans persist selected folder and clip IDs.
10. `random_folder` and `allow_clip_duplication` are now respected.
11. Covers are loaded from Drive `INPUT_ASSETS/covers`.
12. A real default `assets/thumbnail.png` is included; thumbnail height/width settings are respected.
13. Obsolete PIP configuration was removed because PIP is already embedded in Drive video clips.
14. Exact-minute scheduler gating was removed so delayed GitHub scheduled jobs do not silently exit.
15. Disk-space checks and 15-second state heartbeats were added.
16. Chapter generation now receives prior narration context for continuity.

## Verification

- All Python modules compile successfully.
- FFmpeg smoke test confirms background music mixing works.
- Thumbnail generation smoke test passes.
- Static scan confirms the previous critical patterns are absent.
