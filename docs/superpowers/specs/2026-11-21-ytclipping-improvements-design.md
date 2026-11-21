# YtClipping restoration and reliability design

## Status

Approved for implementation. This specification restores the complete CLI and watcher behavior represented by the repository's last committed implementation, with focused reliability improvements.

## Goal

Make YtClipping a stable, minimal working Python application for:

- Turning one pasted YouTube URL into generated Shorts.
- Publishing generated clips through the configured YouTube workflow.
- Optionally monitoring configured YouTube channels and processing new uploads.

This work fixes the damaged working tree rather than adding unrelated features or redesigning the CLI.

## Scope

Restore these implementation surfaces:

- `autoclip_watch.py` as the command-line entry point.
- `watcher/config.py` for configuration loading, saving, and validation.
- `watcher/state.py` for processed-video state.
- `watcher/channels.py` for channel resolution and RSS monitoring.
- `watcher/runner.py` for the AutoClip and split-video pipeline.
- `watcher/transcribe.py` for transcription support.
- `watcher/youtube.py` for direct YouTube OAuth and uploads.
- `watcher/patch_layout.py` for optional AutoClip layout patching.
- `requirements.txt` for installable Python dependencies.

No legacy behavior is removed solely for convenience. Features that remain in the restored implementation must be validated or explicitly reported as unsupported.

## Architecture

The application remains a small Python CLI with thin modules:

- The CLI parses commands and loads environment/configuration.
- Configuration owns typed settings and channel definitions.
- State owns durable video records and retry status.
- Channel helpers resolve channels and return recent RSS entries.
- Runner functions invoke external tools and orchestrate downloads and processing.
- YouTube helpers authenticate and upload directly through Google APIs.

The modules communicate through plain dictionaries and dataclasses. No database or web service is introduced.

## Reliability requirements

### Configuration

- Configuration JSON must be a JSON object.
- Numeric settings must reject booleans and values outside documented bounds.
- Channel IDs and handles must be normalized; malformed or empty entries must not silently become active.
- Platform and portrait-style values must remain bounded to supported values.
- Environment values and API keys must not be logged or written to state.

### State

- State files must be valid JSON objects before use.
- Writes must use a sibling temporary file and atomic replacement.
- Corrupt state must be backed up once and start from an empty state rather than crashing.
- A video is considered processed only after the intended publish or local-production action completes.
- Retry records must distinguish pending, processing, completed, and failed outcomes.

### External processes

- AutoClip and yt-dlp commands must use explicit UTF-8 process handling.
- Every timed process must have a bounded timeout.
- Timeouts and nonzero exits must include concise stderr for diagnosis.
- Child JSON may appear on stdout or stderr, or fall back to a newly created AutoClip project, but stale projects must not be accepted.
- Cleanup must use resolved project paths and must not delete unrelated directories.

### One-off workflow

1. Validate URL, dependencies, privacy, and clip count.
2. Download the complete video or bounded sections for videos longer than six hours.
3. Produce clips with AutoClip.
4. Select only publishable clip IDs up to the requested maximum.
5. Publish when requested; otherwise return locally produced output.
6. Update state and cleanup temporary files or project data only on the documented success/failure path.

### Watch workflow

- Resolve configured channel URLs, handles, and IDs.
- Read channel RSS entries without sharing an API key.
- Respect per-channel and global clip limits.
- Cap new videos per channel in one cycle.
- Persist pending and failed status for retry.
- Avoid duplicate processing across interrupted runs.

## Security

- `config.json`, `state.json`, `.env`, OAuth credentials, and OAuth tokens remain ignored by Git.
- Token files must be permission-restricted on platforms that support it.
- Secrets must not be included in command output or subprocess logs.
- The CLI must report missing credentials and dependency errors as actionable messages rather than stack traces.

## Testing

Add focused standard-library or project-configured tests for:

- Configuration loading and validation.
- State backup, atomic save, status updates, and reset behavior.
- RSS entry parsing and channel-ID validation.
- Child-process JSON parsing and stale-project fallback.
- Top clip selection and publishable clip filtering.
- CLI command dispatch and argument validation.
- One-off and watcher orchestration through mocks, without downloading media or calling external APIs.

## Verification

Implementation is complete only after:

- The dependency manifest installs successfully or missing platform dependencies are explicitly identified.
- Python compilation completes without syntax errors.
- The focused test suite completes with zero failures.
- `python autoclip_watch.py --help` or the project-standard help command completes successfully.
- A Git diff confirms only restoration, hardening, tests, and directly related documentation.
