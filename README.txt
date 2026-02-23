🦈 Sharkey/Misskey Post Archiver


A self-contained desktop application for archiving posts from any Sharkey or Misskey instance. Creates offline mirrors with automatic screenshots, downloads all media, and exports everything to ZIP, preserving posts even if the original instance goes offline.


By downloading and using this program; you agree with the following statements:


Fair Access to Financial Services
We believe that access to financial services is a fundamental right in the modern world. We (and therefore you by using this program), advocate for:


1. Preventing discrimination from financial services based on political views or social media activity.
2. Supporting open banking initiatives and financial technology innovation.
3. Fighting against arbitrary account closures and financial censorship.
4. Promoting privacy-preserving payment systems.



Depoliticizing Internet Service Providers
Internet access has become as essential as electricity or water. We believe that access to financial services is a fundamental right in the modern world. We (and therefore you by using this program) believe in:


1. Maintaining net neutrality principles.
2. Preventing ISPs from becoming arbiters of content.
3. Ensuring equal access regardless of political views.
4. Supporting community-owned Internet infrastructure.
5. Fighting against surveillance and data retention mandates.




Consumer Privacy Protection
In an age of ubiquitous data collection, privacy must be protected. 
We believe that access to financial services is a fundamental right in the modern world. We (and therefore you by using this program) advocate for:


1. Strict limits on data collection and retention.
2. An end to the surveillance advertising model.
3. Right to be forgotten and data portability.
4. Opt-in only data collection.
5. Ban on cross-site tracking without explicit consent.
6. Meaningful penalties for data breaches and misuse.




Copyright Reform and Free Speech
Copyright law should protect creators while preserving free speech. We believe that access to financial services is a fundamental right in the modern world. We (and therefore you by using this program) support:


   1. Reform of DMCA takedown procedures to prevent abuse.
   2. Stronger fair use protections.
   3. Penalties for bad-faith takedown requests.
   4. Protection of transformative works.
   5. Preservation of anonymous speech.
   6. Resistance against automated content filtering.

FEATURES


Core Functionality
No API key required : Archives any public post or user profile without authentication.
Automatic screenshots : Renders each post as a high-quality PNG using headless Chromium.
Full media download : Images, videos, audio, and all attachments saved locally.
Offline HTML mirrors: Self-contained pages that work without internet connection.
ZIP export:  Bundle post metadata (JSON), HTML mirror, screenshots, and media into a single archive.
User Archiving :Download entire post histories (100-1000+ posts) with live progress tracking
Retry Logic:  Automatic retry with exponential backoff for rate limits and server timeouts
Smart Pacing: Respects server load with configurable delays between requests


TECHNICAL
Zero external dependencies: Pure Python standard library (except Playwright for screenshots).
Cross-platform: Works on Windows, macOS, and Linux.
Portable data: SQLite database with full-text search capabilities.
Auto-installer: Prompts to install Playwright on first run if missing.
Standalone .exe: Build a single executable with no Python installation required.


SCREENSHOTS
Main Interface
The archiver runs as a local web app with a clean, responsive UI:


Sidebar:  Paste any post URL, user profile, or Fediverse handle (@user@instance.social).
Post Grid: View all archived posts with thumbnails, metadata, and quick actions.
Live Progress:  Real-time progress bars for bulk archiving operations.


Archived Post View
Each archived post includes:
Full text content with content warnings (if any).
User avatar, display name, and handle.
Inline media gallery (images, video, audio).
Interaction counts (replies, renotes, reactions).
Screenshot of the original post appearance.
Link back to the live version (if still available).


ZIP Archive Contents
```
sharkey_archive_instance.social_noteId123.zip
├── post.json           # Full API response metadata
├── post.html           # Self-contained HTML mirror (works offline)
├── screenshot.png      # PNG screenshot of the post
└── media/
    ├── image1.jpg
    ├── image2.png
    └── video1.mp4
```


Quick Start


Option 1: Run as Python Script


**Requirements:** Python 3.8 or later


```bash
# Clone the repository
git clone https://github.com/yourusername/sharkey-archiver.git
cd sharkey-archiver


# Run the app
python app.py
```


The app will open in your default browser at `http://localhost:5757`.


On first run, you'll be prompted to install Playwright for screenshot support:
```
PLAYWRIGHT NOT INSTALLED?


Playwright is required for automatic post screenshots.
Without it, the archiver still works — screenshots are just skipped.


Install Playwright now? [Y/n]:
```


Press `Y` and it will automatically run:
```bash
pip install playwright
python -m playwright install chromium
```


### Option 2: Build Standalone .exe (Windows)


```powershell
# Install PyInstaller (one-time)
pip install pyinstaller


# Build the executable
pyinstaller --onefile --noconsole --name "SharkeyArchiver" app.py
```


The `.exe` will be in the `dist/` folder. Double-click to run — no Python installation needed on the target machine.


**Note:** Playwright still needs to be installed separately for screenshots. The app will prompt on first run.


Usage Guide


Archive a Single Post


1. Copy any post URL from a Sharkey or Misskey instance
   - Example: `https://misskey.io/notes/abc123xyz`
2. Paste it into the input field
3. Click Archive


The post, all media, and a screenshot are saved immediately.


Archive All Posts from a User


1. Paste a user profile URL or Fediverse handle:
   - `https://sharkey.team/@sharkey
   - `@sharkey@sharkey.team`
   - Just `sharkey` (fill in the Instance field)
2. Set **Max posts** (100 / 250 / 500 / 1000 / All)
3. Click **Archive**


A background job will fetch all public posts with a live progress bar.


Bulk Archive from a List


You can archive multiple posts at once by pasting URLs or note IDs in the input field (separate formats are auto-detected).


Retake Screenshots


If you archived posts before installing Playwright, use the Screenshot Missing Posts button in the sidebar to retroactively screenshot all existing archives.


View & Export


- **View Mirror** — Opens the offline HTML page in a new tab
- **⬇ ZIP** — Downloads a complete archive bundle


Technical Details


### File Structure
```
sharkey-archiver/
├── app.py                   # Main application (single file, ~1300 lines)
└── archive_data/            # Created on first run
    ├── archive.db           # SQLite database
    └── media/               # Downloaded media files
        └── instance.social_noteId/
            ├── fileId1.jpg
            ├── fileId2.mp4
            └── screenshot.png
```


### Database Schema


**posts table:**
- `id` (primary key: `instance.social/noteId`)
- `instance`, `note_id`, `url`
- `user_name`, `user_handle`, `user_avatar`
- `content`, `cw` (content warning)
- `created_at`, `archived_at`
- `reply_count`, `renote_count`, `reaction_count`
- `visibility`, `raw_json`, `screenshot_path`


**media table:**
- `id`, `post_id` (foreign key)
- `filename`, `url`, `mime_type`, `local_path`
- `width`, `height`, `is_sensitive`, `alt_text`


API Endpoints Used


The archiver uses the following public Misskey/Sharkey API endpoints (no authentication required):


- `POST /api/users/show` — Resolve username to user ID
- `POST /api/users/notes` — Fetch posts by user (paginated, max 20 per request)
- `POST /api/notes/show` — Fetch a single post by note ID


All requests include automatic retry logic for `500` errors and implement polite rate limiting (1 second delay between pages).


Screenshot Rendering


Screenshots are generated by:
1. Creating an HTML mirror using the archived post data
2. Serving it via the local HTTP server at `http://127.0.0.1:{port}/render/{token}`
3. Opening the URL in headless Chromium via Playwright
4. Cropping to the `.card` element and saving as PNG


This approach avoids `file://` path issues on Windows and allows media to load via HTTP routes.


Supported Instances


Works with **any** Misskey-protocol server:
Sharkey
Misskey
Firefish (formerly Calckey)
IceShrimp
Foundkey
Magnetar
Meisskey
CherryPick


Tested on public instances including `sharkey.team`, `eepy.moe`, `misskey.io`, `mk.absturztau.be`, and many others.


Limitations


Public posts only: Private/followers-only posts require API authentication (not currently supported).
No thread reconstruction: Archives individual posts; doesn't automatically fetch reply chains.
Rate limits: Some instances may throttle or block rapid bulk requests (use lower Max Posts setting).
Large archives : Archiving 1000+ posts with many media files can take 10-30 minutes.
Playwright dependency** — Chromium download is ~150 MB (one-time, per user profile).






License


GNU Affero General Public License. See LICENSE file for details.


Acknowledgments


Built for the Fediverse community
Uses [Playwright](https://playwright.dev/) for browser automation
Inspired by [GhostArchive](https://ghostarchive.org/) and similar web archiving tools


Legal & Ethics


This tool is intended for:
 Personal backup of your own posts.
 Archiving public posts for research, journalism, or documentation.
Preserving content from instances at risk of shutdown.


Please use responsibly!
Respect copyright and terms of service.
Don't archive private/sensitive content without permission.
Don't use it for harassment, doxxing, or malicious purposes.
Be mindful of server load when bulk archiving.


---


**Questions or issues?** Open an issue on GitHub