# Changelog

All notable changes to Sharkey/Misskey Post Archiver will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-02-23

### Added
- Initial release
- Archive individual posts by URL or note ID
- Archive entire user post histories (up to 1000+ posts)
- Automatic screenshot generation using Playwright
- Full media download (images, videos, audio)
- Offline HTML mirrors with embedded or linked media
- ZIP export with post metadata, HTML, screenshots, and media
- SQLite database for persistent storage
- Auto-installer for Playwright on first run
- Retry logic with exponential backoff for rate limits
- Smart pacing (1s delay between pages) to respect server load
- Progress tracking for bulk operations
- Retake screenshots feature for existing archives
- Cross-platform support (Windows, macOS, Linux)
- Standalone .exe build support via PyInstaller
- No API key required for public posts
- Support for all Misskey-protocol servers (Sharkey, Misskey, Firefish, etc.)

### Technical
- Pure Python standard library (zero dependencies except Playwright)
- Local web UI running on http://localhost:5757
- Responsive design with dark theme
- Content warning support with click-to-reveal
- Sensitive media blur with hover-to-show
- Automatic instance detection from URLs and Fediverse handles

## [Unreleased]

### Planned Features
- Thread reconstruction (fetch entire reply chains)
- Authenticated archiving for private posts
- Search and filtering in the UI
- Export to Markdown/PDF formats
- Scheduled auto-archiving
- Bluesky/AT Protocol support
