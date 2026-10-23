# 声麦 Studio Pro Privacy Policy

**Effective date: 2026-10-22**
**Applies to: 声麦 Studio Pro 1.0 and later**
**Developer and publisher: IMXia-Chan**

## 1. Summary

SingMic Studio is a recording and mixing application that runs entirely on the user's own computer. It has **no user accounts, no usage analytics, and no telemetry**. This policy describes what data the application touches, where it is stored, and when it may leave the device.

## 2. Data that stays on the device

The following is stored locally only, and is never uploaded, synced, or backed up to any server:

- Recordings, imported audio, generated backing tracks, and exported files;
- Project files, media library index, and render cache;
- Interface language, shortcuts, and preferences;
- Any API key entered by the user (stored under `%USERPROFILE%\.singmic\` and readable only by local processes).

The installed application stores data in `%LOCALAPPDATA%\SingMic\data`. Uninstalling does not automatically remove this folder; the user may delete it.

## 3. Microphone

The application accesses the microphone when the user starts recording or enables pitch and tempo analysis. Audio is written to local files only and is not transmitted. Microphone access is always initiated by an explicit user action; the application does not record in the background.

## 4. Network features

The application works without a network connection. The features below are **initiated by the user** and use only services and keys that the user configures:

| Feature | Data sent | Recipient |
|---|---|---|
| AI accompaniment generation | The style description, lyrics, duration, and key entered by the user | The AI music platform the user selected and configured with their own API key |
| Mixing assistant (natural-language parameter control) | The user's instruction, plus **track and clip parameters** from the project (gain, pan, plug-in settings, pitch offsets) | The DeepSeek endpoint configured by the user |
| Voice extension | **The audio segment the user selected** (15 seconds or less) | The inference server the user set up (runs locally when none is configured) |

Explicitly not sent: full songs, audio from other tracks, file paths, file names, project names, or any device identifier. If a service is not configured, its feature is unavailable and the application does not connect to any server on its own.

AI accompaniment generation and the mixing assistant forward content to third-party platforms, whose own privacy policies apply to that processing.

## 5. Models

Speech recognition and AI noise reduction use local models. Model files are downloaded from public sources after user confirmation, and the download carries no user data.

## 6. Children

The application does not knowingly collect any information from children and has no account system.

## 7. Deleting data

Deleting project files, media files, or the entire data folder removes the corresponding data. The application keeps no copy of user data on any server.

## 8. Changes

Changes to this policy will be published with a software update, and the effective date on this page will be revised.

## 9. Contact

- Project home: https://github.com/IMXia-Chan
- Support email: singmic.studio@foxmail.com
- Microsoft Store listing: leave a review or a message on the store page for 声麦 Studio Pro
